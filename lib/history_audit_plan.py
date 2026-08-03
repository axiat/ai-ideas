"""Deterministic capacity and budget planning for history audit v2."""

import copy
import datetime
import hashlib

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


def _canonical_sha(domain, value):
    return history_contract_v2.framed_sha256(
        domain, history_contract_v2.canonical_bytes(value)
    )


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
    return _canonical_sha(
        "history-l2-snapshot-records-v2", runtime_snapshot_records(records)
    )


def runtime_shard_plan_sha(shards):
    if not isinstance(shards, list) or not shards:
        raise AuditPlanError("invalid_runtime_shards")
    normalized = []
    for shard in shards:
        if not isinstance(shard, dict) or set(shard) != {
            "shard_id", "item_ids", "request_sha256", "serialized_request"
        }:
            raise AuditPlanError("invalid_runtime_shards")
        if (
            not isinstance(shard["shard_id"], str)
            or not shard["shard_id"]
            or not isinstance(shard["serialized_request"], str)
            or not _is_sha(shard["request_sha256"])
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


def runtime_budget_policy_sha(policy):
    if not isinstance(policy, dict):
        raise AuditPlanError("invalid_budget")
    return _canonical_sha("history-budget-policy-v1", policy)


_RUNTIME_PLAN_FIELDS = frozenset(
    {
        "schema_version", "run_id", "batch_id", "candidate", "snapshot",
        "provider_pools_ordered", "provider_capability_profile_hashes",
        "capacity_profile", "budget_policy", "budget_policy_sha", "intent",
        "risk_policy_sha", "settlement_policy_sha", "shard_plan_sha", "shards",
    }
)


def build_runtime_plan_material(plan):
    if not isinstance(plan, dict):
        raise AuditPlanError("invalid_runtime_plan")
    try:
        candidate = copy.deepcopy(plan["candidate"])
        snapshot = copy.deepcopy(plan["snapshot"])
        snapshot.pop("records")
        snapshot["records_sha"] = runtime_snapshot_records_sha(
            plan["snapshot"]["records"]
        )
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
            "capacity_profile": copy.deepcopy(plan["capacity_profile"]),
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
    except (KeyError, TypeError) as exc:
        raise AuditPlanError("invalid_runtime_plan") from exc
    validate_runtime_plan_material(material)
    return material


def validate_runtime_plan_material(material):
    if not isinstance(material, dict) or set(material) != _RUNTIME_PLAN_FIELDS:
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
    if (
        snapshot["snapshot_hash"] != expected_snapshot_hash
        or snapshot["snapshot_id"] != expected_snapshot_id
    ):
        raise AuditPlanError("invalid_runtime_snapshot")
    intent_policy = _intent_policy(material["budget_policy"], material["intent"])
    if (
        runtime_budget_policy_sha(material["budget_policy"])
        != material["budget_policy_sha"]
        or material["budget_policy"]["risk_policy_sha"]
        != material["risk_policy_sha"]
        or material["budget_policy"]["settlement_policy_sha"]
        != material["settlement_policy_sha"]
        or not intent_policy
    ):
        raise AuditPlanError("invalid_budget")
    if runtime_shard_plan_sha(material["shards"]) != material["shard_plan_sha"]:
        raise AuditPlanError("invalid_runtime_shards")
    assigned_ids = [
        item_id for shard in material["shards"] for item_id in shard["item_ids"]
    ]
    if sorted(assigned_ids) != snapshot["expected_asset_ids"]:
        raise AuditPlanError("invalid_runtime_shards")
    capacity = material["capacity_profile"]
    if (
        not isinstance(capacity, dict)
        or type(capacity.get("max_output_tokens")) is not int
        or capacity["max_output_tokens"] < 0
    ):
        raise AuditPlanError("invalid_capacity_profile")
    profiles = material["provider_capability_profile_hashes"]
    if (
        not isinstance(profiles, list)
        or not profiles
        or len(set(profiles)) != len(profiles)
        or any(not _is_sha(profile) for profile in profiles)
    ):
        raise AuditPlanError("invalid_provider_capabilities")
    _validate_pools(material["provider_pools_ordered"])
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
        if not isinstance(actual_usage, dict) or set(actual_usage).difference(_RESOURCE_FIELDS):
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
    plan_material = {
        "schema_version": "history-audit-plan-v1",
        "snapshot": copy.deepcopy(snapshot),
        "candidate": copy.deepcopy(candidate),
        "provider_pools_ordered": copy.deepcopy(provider_pools),
        "provider_capability_profile_hashes": capability_hashes,
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
        "plan_sha": plan_sha,
        "capacity_profile_id": capacity_profile["profile_id"],
        "base_capacity_profile_id": capacity_profile["base_profile_id"],
        "provider_pools_ordered": copy.deepcopy(provider_pools),
        "provider_capability_profile_hashes": capability_hashes,
        "pool_bounds": pool_bounds,
        "b_pool": b_pool,
        "b_target": b_target,
        "shard_plan_sha": shard_plan_sha,
        "shards": planned_shards,
        "logical_task_keys": logical_tasks,
        "budget_events": reserved_events,
    }


def attempt_manifest(plan, shard_index, ordinal, capability):
    """Bind actual provider provenance without changing logical task identity."""
    if type(shard_index) is not int or type(ordinal) is not int:
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
