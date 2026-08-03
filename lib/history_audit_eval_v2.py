#!/usr/bin/env python3
"""Semantic qrels, release qualification, routing, and event cost summaries."""

import copy
import datetime
import hashlib
import json
import math
import re

try:
    from lib import history_contract_v2 as contract
except ImportError:
    import history_contract_v2 as contract


QREL_SCHEMA = "history-audit-qrel-v2"
POLICY_SCHEMA = "semantic-release-policy-v1"
SEMANTIC_RELATIONS = frozenset(
    {"blocking_duplicate", "substantive_overlap", "related_only", "distinct", "uncertain"}
)
_PARTITIONS = ("train", "development", "test")
_QREL_FIELDS = frozenset(
    {
        "schema_version", "qrel_id", "query_id", "query_lineage_id",
        "historical_id", "historical_lineage_id", "temporal_group",
        "as_of_sequence", "historical_sequence", "semantic_relation",
        "historical_text", "evidence_anchors", "adjudication_state",
        "negative_kind", "risk_slices", "partition", "scope",
    }
)
_SHA = re.compile(r"^[0-9a-f]{64}$")
_ROUTE_SEVERITY = {"routine": 0, "guarded": 1, "exhaustive": 2}
_REQUIRED_EVIDENCE_FIELDS = frozenset(
    {
        "no_match_basis", "corpus_snapshot_hash", "evaluation_hash",
        "metric_report_hash", "dependency_hashes", "provider_capacity_complete",
        "fault_evidence_passed", "replay_evidence_passed", "expires_at",
    }
)
_REQUIRED_DEPENDENCIES = frozenset(
    {
        "semantic_policy", "prompt", "schema", "ordered_provider_pools",
        "capacity", "provider", "fault", "replay",
    }
)
_ONE_SIDED_95_Z = 1.6448536269514722
_CRITICAL_SLICES = frozenset(
    {"low_overlap", "cross_language", "lineage_revision"}
)


def _hash(domain, value):
    return contract.framed_sha256(domain, contract.canonical_bytes(value))


def _decimal_identity(value):
    """Represent policy decimals as exact text before using the v2 codec."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("policy decimals must be finite")
        return format(value, ".17g")
    if isinstance(value, list):
        return [_decimal_identity(item) for item in value]
    if isinstance(value, dict):
        return {key: _decimal_identity(item) for key, item in value.items()}
    return value


def _policy_hash(policy):
    return _hash("semantic-release-policy-v1", _decimal_identity(policy))


def semantic_policy_sha256(policy):
    """Return the exact fixed-decimal identity of a validated release policy."""
    return _policy_hash(_validate_policy(policy))


def _require_sha(value, name):
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError("%s must be a lowercase SHA-256" % name)
    return value


def _timestamp(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a timezone-aware timestamp" % name)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("%s must be a timezone-aware timestamp" % name) from exc
    if parsed.tzinfo is None:
        raise ValueError("%s must be a timezone-aware timestamp" % name)
    return parsed.astimezone(datetime.timezone.utc)


def _validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {
        "schema_version", "semantic_policy_profile_id", "wilson_one_sided_z",
        "shadow", "production",
    }:
        raise ValueError("semantic release policy fields are invalid")
    if (
        policy["schema_version"] != POLICY_SCHEMA
        or not isinstance(policy["semantic_policy_profile_id"], str)
        or not policy["semantic_policy_profile_id"]
        or not isinstance(policy["wilson_one_sided_z"], (int, float))
        or isinstance(policy["wilson_one_sided_z"], bool)
        or policy["wilson_one_sided_z"] != _ONE_SIDED_95_Z
    ):
        raise ValueError("semantic release policy identity is invalid")
    shadow = policy["shadow"]
    if not isinstance(shadow, dict) or set(shadow) != {
        "minimum_positive_lineages", "minimum_negative_lineages", "critical_slices"
    }:
        raise ValueError("shadow policy fields are invalid")
    for field in ("minimum_positive_lineages", "minimum_negative_lineages"):
        if type(shadow[field]) is not int or shadow[field] < 0:
            raise ValueError("shadow threshold is invalid")
    if (
        shadow["minimum_positive_lineages"] < 30
        or shadow["minimum_negative_lineages"] < 20
        or not _CRITICAL_SLICES.issubset(shadow["critical_slices"])
        or any(shadow["critical_slices"][name] < 5 for name in _CRITICAL_SLICES)
    ):
        raise ValueError("shadow policy lowers a contractual minimum")
    if (
        not isinstance(shadow["critical_slices"], dict)
        or not shadow["critical_slices"]
        or any(
            not isinstance(name, str) or not name
            or type(minimum) is not int or minimum < 0
            for name, minimum in shadow["critical_slices"].items()
        )
    ):
        raise ValueError("critical slice thresholds are invalid")
    production = policy["production"]
    if not isinstance(production, dict) or set(production) != {
        "minimum_positive_lineages", "aggregate", "required_slices"
    }:
        raise ValueError("production policy fields are invalid")
    if type(production["minimum_positive_lineages"]) is not int or production["minimum_positive_lineages"] < 0:
        raise ValueError("production positive threshold is invalid")
    if (
        production["minimum_positive_lineages"] < 300
        or not _CRITICAL_SLICES.issubset(production["required_slices"])
    ):
        raise ValueError("production policy lowers a contractual minimum")
    for metric_policy in [production["aggregate"]] + list(production["required_slices"].values()):
        if not isinstance(metric_policy, dict) or set(metric_policy) != {
            "minimum_observations", "minimum_recall_lower_bound",
            "maximum_false_negative_upper_bound",
        }:
            raise ValueError("production metric gate is invalid")
        if type(metric_policy["minimum_observations"]) is not int or metric_policy["minimum_observations"] < 1:
            raise ValueError("production observation threshold is invalid")
        for name in ("minimum_recall_lower_bound", "maximum_false_negative_upper_bound"):
            value = metric_policy[name]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 or value > 1:
                raise ValueError("production metric bound is invalid")
    return copy.deepcopy(policy)


def validate_qrels(rows, partitions, *, scope):
    """Validate closed qrels and reject lineage, time, judgment, or scope leakage."""
    if not isinstance(scope, str) or not scope:
        raise ValueError("qrels scope is required")
    if not isinstance(rows, list) or not rows:
        raise ValueError("qrels must be a nonempty array")
    if not isinstance(partitions, dict) or set(partitions) != set(_PARTITIONS):
        raise ValueError("qrel partitions must be train, development, and test")
    partition_lineages = {}
    for partition in _PARTITIONS:
        values = partitions[partition]
        if (
            not isinstance(values, list)
            or len(values) != len(set(values))
            or any(not isinstance(value, str) or not value for value in values)
        ):
            raise ValueError("qrel partition lineages are invalid")
        for lineage_id in values:
            if lineage_id in partition_lineages:
                raise ValueError("query lineage leaks across partitions")
            partition_lineages[lineage_id] = partition
    normalized = []
    qrel_ids = set()
    judgments = set()
    query_partition = {}
    historical_partition = {}
    temporal_partition = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != _QREL_FIELDS:
            raise ValueError("qrel schema is closed")
        if row["schema_version"] != QREL_SCHEMA or row["scope"] != scope:
            raise ValueError("qrel scope or schema is invalid")
        for field in (
            "qrel_id", "query_id", "query_lineage_id", "historical_id",
            "historical_lineage_id", "temporal_group", "historical_text",
        ):
            if not isinstance(row[field], str) or not row[field]:
                raise ValueError("qrel identity or text is invalid")
        if row["qrel_id"] in qrel_ids:
            raise ValueError("qrel judgment is duplicated")
        qrel_ids.add(row["qrel_id"])
        pair = (row["query_id"], row["historical_id"])
        if pair in judgments:
            raise ValueError("qrel judgment is duplicated")
        judgments.add(pair)
        if (
            type(row["as_of_sequence"]) is not int
            or type(row["historical_sequence"]) is not int
            or row["historical_sequence"] < 0
            or row["as_of_sequence"] < row["historical_sequence"]
        ):
            raise ValueError("qrel exposes future history")
        if row["semantic_relation"] not in SEMANTIC_RELATIONS:
            raise ValueError("qrel semantic relation is invalid")
        if row["adjudication_state"] not in {"adjudicated", "pending"}:
            raise ValueError("qrel adjudication state is invalid")
        if row["negative_kind"] not in {None, "hard_negative", "true_no_match"}:
            raise ValueError("qrel negative kind is invalid")
        if row["semantic_relation"] == "blocking_duplicate" and row["negative_kind"] is not None:
            raise ValueError("positive qrel cannot be a negative")
        if row["negative_kind"] is not None and row["semantic_relation"] != "distinct":
            raise ValueError("negative qrel must use the shared distinct relation")
        anchors = row["evidence_anchors"]
        if (
            not isinstance(anchors, list) or not anchors
            or len(anchors) != len(set(anchors))
            or any(not isinstance(anchor, str) or not anchor or anchor not in row["historical_text"] for anchor in anchors)
        ):
            raise ValueError("qrel evidence anchors are not extractive")
        slices = row["risk_slices"]
        if (
            not isinstance(slices, list) or slices != sorted(slices)
            or len(slices) != len(set(slices))
            or any(not isinstance(value, str) or not value for value in slices)
        ):
            raise ValueError("qrel risk slices are invalid")
        partition = row["partition"]
        if partition not in _PARTITIONS or partition_lineages.get(row["query_lineage_id"]) != partition:
            raise ValueError("qrel partition membership is inconsistent")
        for mapping, identity, message in (
            (query_partition, row["query_lineage_id"], "query lineage leaks across partitions"),
            (historical_partition, row["historical_lineage_id"], "historical lineage leaks across partitions"),
            (temporal_partition, row["temporal_group"], "temporal group leaks across partitions"),
        ):
            prior = mapping.setdefault(identity, partition)
            if prior != partition:
                raise ValueError(message)
        normalized.append(copy.deepcopy(row))
    declared = set(partition_lineages)
    observed = {row["query_lineage_id"] for row in normalized}
    if declared != observed:
        raise ValueError("qrel partitions do not exactly cover query lineages")
    normalized.sort(key=lambda item: item["qrel_id"])
    normalized_partitions = {name: sorted(partitions[name]) for name in _PARTITIONS}
    material = {
        "schema_version": "validated-history-audit-qrels-v2",
        "scope": scope,
        "partitions": normalized_partitions,
        "rows": normalized,
    }
    material["qrels_hash"] = _hash("history-audit-qrels-v2", material)
    return material


def _validate_dataset(dataset):
    if not isinstance(dataset, dict) or set(dataset) != {
        "schema_version", "scope", "partitions", "rows", "qrels_hash"
    }:
        raise ValueError("validated qrels are required")
    if dataset["schema_version"] != "validated-history-audit-qrels-v2":
        raise ValueError("validated qrels schema is invalid")
    expected = copy.deepcopy(dataset)
    expected.pop("qrels_hash")
    if dataset["qrels_hash"] != _hash("history-audit-qrels-v2", expected):
        raise ValueError("validated qrels hash is invalid")
    return dataset


def _validate_outputs(dataset, outputs):
    if not isinstance(outputs, list):
        raise ValueError("evaluation outputs must be an array")
    expected = {
        (row["query_id"], row["historical_id"])
        for row in dataset["rows"]
    }
    observed = {}
    for row in outputs:
        if not isinstance(row, dict) or set(row) != {
            "query_id", "historical_id", "semantic_relation"
        }:
            raise ValueError("evaluation output schema is closed")
        pair = (row["query_id"], row["historical_id"])
        if pair in observed or row["semantic_relation"] not in SEMANTIC_RELATIONS:
            raise ValueError("evaluation output is duplicated or invalid")
        observed[pair] = row["semantic_relation"]
    if set(observed) != expected:
        raise ValueError("evaluation outputs require exact query coverage")
    return observed


def _lineage_outcomes(dataset, output_by_query):
    positive = {}
    negative = set()
    slices = {}
    for row in dataset["rows"]:
        if row["adjudication_state"] != "adjudicated":
            continue
        lineage_id = row["query_lineage_id"]
        if row["semantic_relation"] == "blocking_duplicate":
            detected = output_by_query[(row["query_id"], row["historical_id"])] in {
                "blocking_duplicate", "substantive_overlap"
            }
            positive[lineage_id] = positive.get(lineage_id, True) and detected
            slices.setdefault(lineage_id, set()).update(row["risk_slices"])
        elif row["negative_kind"] is not None:
            negative.add(lineage_id)
    return positive, negative, slices


def _wilson(successes, denominator, z):
    if type(successes) is not int or type(denominator) is not int or denominator < 0 or successes < 0 or successes > denominator:
        raise ValueError("Wilson counts are invalid")
    if denominator == 0:
        return {"numerator": successes, "denominator": denominator, "lower_bound": None, "upper_bound": None, "z": z}
    proportion = successes / denominator
    z2 = z * z
    centre = proportion + z2 / (2 * denominator)
    radius = z * math.sqrt((proportion * (1 - proportion) + z2 / (4 * denominator)) / denominator)
    scale = 1 + z2 / denominator
    return {
        "numerator": successes,
        "denominator": denominator,
        "lower_bound": max(0.0, (centre - radius) / scale),
        "upper_bound": min(1.0, (centre + radius) / scale),
        "z": z,
    }


def _metric(successes, denominator, z):
    recall = _wilson(successes, denominator, z)
    false_negative = _wilson(denominator - successes, denominator, z)
    return {
        "recall": recall,
        "false_negative": false_negative,
    }


def evaluate_shadow_readiness(qrels, outputs, policy):
    """Return diagnostic readiness from independent adjudicated lineages."""
    dataset = _validate_dataset(qrels)
    normalized_policy = _validate_policy(policy)
    output_by_query = _validate_outputs(dataset, outputs)
    positive, negative, slices = _lineage_outcomes(dataset, output_by_query)
    counts = {
        "positive_lineages": len(positive),
        "negative_lineages": len(negative),
        "critical_slices": {
            name: sum(name in lineage_slices for lineage_slices in slices.values())
            for name in sorted(normalized_policy["shadow"]["critical_slices"])
        },
    }
    vetoes = []
    shadow = normalized_policy["shadow"]
    if counts["positive_lineages"] < shadow["minimum_positive_lineages"]:
        vetoes.append("insufficient_positive_lineages")
    if counts["negative_lineages"] < shadow["minimum_negative_lineages"]:
        vetoes.append("insufficient_negative_lineages")
    for name, minimum in sorted(shadow["critical_slices"].items()):
        if counts["critical_slices"].get(name, 0) < minimum:
            vetoes.append("slice_%s_underpowered" % name)
    successes = sum(positive.values())
    z = normalized_policy["wilson_one_sided_z"]
    return {
        "readiness_state": "not_ready" if vetoes else "shadow_ready",
        "production_qualified": False,
        "scope": dataset["scope"],
        "counts": counts,
        "intervals": _metric(successes, len(positive), z),
        "vetoes": vetoes,
        "qrels_hash": dataset["qrels_hash"],
        "policy_sha256": _policy_hash(normalized_policy),
    }


def _validate_evidence(evidence, policy_sha):
    if not isinstance(evidence, dict) or set(evidence) != _REQUIRED_EVIDENCE_FIELDS:
        raise ValueError("production evidence fields are invalid")
    if evidence["no_match_basis"] not in {"l1_calibrated", "l2_exhaustive"}:
        raise ValueError("production no-match basis is invalid")
    for field in ("corpus_snapshot_hash", "evaluation_hash", "metric_report_hash"):
        _require_sha(evidence[field], field)
    dependencies = evidence["dependency_hashes"]
    if (
        not isinstance(dependencies, dict)
        or not _REQUIRED_DEPENDENCIES.issubset(dependencies)
        or any(not isinstance(name, str) or not name or _SHA.fullmatch(value or "") is None for name, value in dependencies.items())
    ):
        raise ValueError("qualification dependency hashes are invalid")
    for field in ("provider_capacity_complete", "fault_evidence_passed", "replay_evidence_passed"):
        if type(evidence[field]) is not bool:
            raise ValueError("production evidence gate is invalid")
    _timestamp(evidence["expires_at"], "expires_at")
    result = copy.deepcopy(evidence)
    if result["dependency_hashes"]["semantic_policy"] != policy_sha:
        raise ValueError("semantic policy dependency is not exact")
    return result


def _apply_metric_gate(metric, gate):
    denominator = metric["recall"]["denominator"]
    if denominator < gate["minimum_observations"]:
        return "abstain"
    if (
        metric["recall"]["lower_bound"] < gate["minimum_recall_lower_bound"]
        or metric["false_negative"]["upper_bound"] > gate["maximum_false_negative_upper_bound"]
    ):
        return "failed"
    return "passed"


def evaluate_production_qualification(qrels, outputs, policy, evidence):
    """Produce an immutable qualification candidate or an exact veto list."""
    dataset = _validate_dataset(qrels)
    normalized_policy = _validate_policy(policy)
    policy_sha = _policy_hash(normalized_policy)
    normalized_evidence = _validate_evidence(evidence, policy_sha)
    output_by_query = _validate_outputs(dataset, outputs)
    positive, negative, slices = _lineage_outcomes(dataset, output_by_query)
    z = normalized_policy["wilson_one_sided_z"]
    aggregate = _metric(sum(positive.values()), len(positive), z)
    aggregate["state"] = _apply_metric_gate(aggregate, normalized_policy["production"]["aggregate"])
    slice_metrics = {}
    vetoes = []
    for name, gate in sorted(normalized_policy["production"]["required_slices"].items()):
        members = [lineage for lineage in positive if name in slices.get(lineage, set())]
        metric = _metric(sum(1 for lineage in members if positive[lineage]), len(members), z)
        metric["state"] = _apply_metric_gate(metric, gate)
        slice_metrics[name] = metric
        if metric["state"] != "passed":
            vetoes.append("slice_%s_%s" % (name, metric["state"]))
    if dataset["scope"] not in {"real", "production", "real_qrels"}:
        vetoes.append("non_production_scope")
    if len(positive) < normalized_policy["production"]["minimum_positive_lineages"]:
        vetoes.append("insufficient_positive_lineages")
    if aggregate["state"] != "passed":
        vetoes.append("aggregate_%s" % aggregate["state"])
    if not normalized_evidence["provider_capacity_complete"]:
        vetoes.append("provider_capacity_incomplete")
    if not normalized_evidence["fault_evidence_passed"]:
        vetoes.append("fault_evidence_failed")
    if not normalized_evidence["replay_evidence_passed"]:
        vetoes.append("replay_evidence_failed")
    vetoes = sorted(set(vetoes))
    metrics = {
        "aggregate_recall": aggregate["recall"],
        "aggregate_false_negative": aggregate["false_negative"],
        "aggregate_state": aggregate["state"],
        "slices": {
            name: {
                "recall": metric["recall"],
                "false_negative": metric["false_negative"],
                "state": metric["state"],
            }
            for name, metric in slice_metrics.items()
        },
        "negative_lineages": len(negative),
    }
    return {
        "schema_version": "semantic-qualification-v2",
        "semantic_policy_profile_id": normalized_policy["semantic_policy_profile_id"],
        "production_qualified": not vetoes,
        "no_match_basis": normalized_evidence["no_match_basis"],
        "scope": dataset["scope"],
        "policy_sha256": policy_sha,
        "qrels_hash": dataset["qrels_hash"],
        "corpus_snapshot_hash": normalized_evidence["corpus_snapshot_hash"],
        "evaluation_hash": normalized_evidence["evaluation_hash"],
        "metric_report_hash": normalized_evidence["metric_report_hash"],
        "dependency_hashes": normalized_evidence["dependency_hashes"],
        "metrics": metrics,
        "vetoes": vetoes,
        "expires_at": normalized_evidence["expires_at"],
    }


def invalidate_qualification(qualification, changed_dependencies):
    """Describe dependency-local invalidation without changing source artifacts."""
    if not isinstance(qualification, dict) or not isinstance(qualification.get("dependency_hashes"), dict):
        raise ValueError("qualification dependencies are invalid")
    if not isinstance(changed_dependencies, dict) or not changed_dependencies:
        raise ValueError("changed dependencies are required")
    bound = qualification["dependency_hashes"]
    changed = sorted(name for name, value in changed_dependencies.items() if bound.get(name) != value)
    semantic_kinds = {
        "semantic_policy", "prompt", "schema", "ordered_provider_pools",
        "capacity", "provider", "fault", "replay", "corpus", "evaluation",
        "fts", "metadata", "embedding", "tokenizer",
    }
    qualification_stale = any(name in semantic_kinds and name in bound for name in changed)
    adjudication_stale = any(name in {"prompt", "schema", "ordered_provider_pools", "capacity", "provider"} for name in changed)
    search = []
    if "fts" in changed:
        search.append("fts")
    if "metadata" in changed:
        search.append("metadata")
    if "embedding" in changed:
        search.append("embedding")
    if "tokenizer" in changed:
        search.append("tokenizer")
    return {
        "qualification_stale": qualification_stale,
        "adjudication_stale": adjudication_stale,
        "search_generations_stale": search,
        "flat_generation_stale": "fts" in search,
        "changed_bound_dependencies": [name for name in changed if name in bound],
    }


def _rule_table(ordered_rules):
    if not isinstance(ordered_rules, dict) or set(ordered_rules) != {
        "schema_version", "risk_policy_version", "rules"
    }:
        raise ValueError("risk policy fields are invalid")
    if ordered_rules["schema_version"] != "history-risk-policy-v1" or not isinstance(ordered_rules["risk_policy_version"], str) or not ordered_rules["risk_policy_version"]:
        raise ValueError("risk policy identity is invalid")
    rules = ordered_rules["rules"]
    if not isinstance(rules, list) or not rules:
        raise ValueError("risk rules are required")
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"rule_id", "fact", "equals", "required_route", "pre_l1"}:
            raise ValueError("risk rule schema is closed")
        if (
            not isinstance(rule["rule_id"], str) or not rule["rule_id"] or rule["rule_id"] in seen
            or not isinstance(rule["fact"], str) or not rule["fact"]
            or type(rule["equals"]) is not bool
            or rule["required_route"] not in _ROUTE_SEVERITY
            or type(rule["pre_l1"]) is not bool
        ):
            raise ValueError("risk rule is invalid")
        seen.add(rule["rule_id"])
    return rules


def route_candidate(facts, ordered_rules):
    """Apply an ordered model-free rule table and preserve external gates."""
    required_facts = {
        "retriever_calibrated", "finalist_or_sa", "mandatory_channel_failed",
        "comparator_uncertain", "bad_slice_membership",
        "index_profile_recently_changed", "permanent_no_match_requested",
        "release_qualified", "candidate_budget_available", "attempt_budget_available",
    }
    if not isinstance(facts, dict) or set(facts) != required_facts or any(type(value) is not bool for value in facts.values()):
        raise ValueError("router facts are closed booleans")
    rules = _rule_table(ordered_rules)
    rule_table_sha = _hash("history-risk-rule-table-v1", rules)
    effective_facts = dict(facts)
    effective_facts["permanent_no_match_without_release_gate"] = (
        facts["permanent_no_match_requested"] and not facts["release_qualified"]
    )
    matched = []
    route = "routine"
    pre_l1 = False
    for rule in rules:
        if effective_facts.get(rule["fact"]) == rule["equals"]:
            matched.append(rule["rule_id"])
            if _ROUTE_SEVERITY[rule["required_route"]] > _ROUTE_SEVERITY[route]:
                route = rule["required_route"]
            pre_l1 = pre_l1 or rule["pre_l1"]
    dispatch_allowed = facts["candidate_budget_available"] and facts["attempt_budget_available"]
    receipt_version = "%s@%s" % (ordered_rules["risk_policy_version"], rule_table_sha)
    return {
        "route": route,
        "matched_rule_ids": matched,
        "call_l1_model": not pre_l1,
        "dispatch_allowed": dispatch_allowed,
        "release_authorized": facts["release_qualified"],
        "rule_table_sha256": rule_table_sha,
        "receipt_risk_policy_version": receipt_version,
        "receipt_binding": {
            "risk_policy_version": receipt_version,
            "matched_router_rule_ids": matched,
        },
    }


def _event_index(events, expected_type, name):
    if not isinstance(events, list):
        raise ValueError("%s must be an event array" % name)
    result = {}
    event_ids = set()
    for event in events:
        if not isinstance(event, dict) or event.get("event_type") != expected_type:
            raise ValueError("%s contains an invalid event" % name)
        event_id = event.get("event_id")
        attempt_id = event.get("attempt_id")
        if not isinstance(event_id, str) or not event_id or event_id in event_ids or not isinstance(attempt_id, str) or not attempt_id or attempt_id in result:
            raise ValueError("%s contains a duplicate identity" % name)
        event_ids.add(event_id)
        result[attempt_id] = event
    return result


def _empty_cost():
    return {
        "calls": 0, "failed_calls": 0, "retry_calls": 0, "failover_calls": 0,
        "split_calls": 0, "detail_calls": 0, "reduce_calls": 0,
        "billable_cancelled_calls": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_tokens": 0, "provider_usage_units": 0, "queue_latency_ms": 0,
        "run_latency_ms": 0,
    }


def _add_cost(target, source, scale=1.0):
    for field in (
        "calls", "input_tokens", "output_tokens", "cache_tokens",
        "provider_usage_units", "queue_latency_ms", "run_latency_ms",
    ):
        target[field] = target.get(field, 0) + source.get(field, 0) * scale
    if "currency_micros" in target and "currency_micros" in source:
        target["currency_micros"] += source["currency_micros"] * scale


def summarize_realized_cost(attempts, candidates):
    """Aggregate immutable attempt/reservation/settlement events exactly once."""
    del candidates
    if not isinstance(attempts, dict) or set(attempts) != {"attempt_events", "budget_events", "settlement_events"}:
        raise ValueError("cost input requires append-only event ledgers")
    started = _event_index(attempts["attempt_events"], "attempt_started", "attempt_events")
    reserved = _event_index(attempts["budget_events"], "attempt_reserved", "budget_events")
    settled = _event_index(attempts["settlement_events"], "attempt_settled", "settlement_events")
    if set(started) != set(reserved) or not set(settled).issubset(started):
        raise ValueError("cost event ledgers do not bind exact attempts")
    per_intent = {}
    stage_costs = {}
    candidate_stages = {}
    currency_known = {}
    for attempt_id, start in started.items():
        required = {"event_id", "event_type", "attempt_id", "intent", "candidate_id", "stage", "attempt_kind"}
        if set(start) != required or start["stage"] not in {"l1", "l2"} or start["attempt_kind"] not in {"initial", "retry", "failover", "split", "detail", "reduce"}:
            raise ValueError("started attempt schema is invalid")
        for field in ("intent", "candidate_id"):
            if not isinstance(start[field], str) or not start[field]:
                raise ValueError("started attempt identity is invalid")
        reservation = reserved[attempt_id]
        if set(reservation) != {"event_id", "event_type", "attempt_id", "reserved"} or not isinstance(reservation["reserved"], dict):
            raise ValueError("budget reservation schema is invalid")
        settlement = settled.get(attempt_id)
        usage = reservation["reserved"]
        queue_latency = 0
        run_latency = 0
        outcome = "unsettled"
        billable = True
        known_price = False
        if settlement is not None:
            required_settlement = {
                "event_id", "event_type", "attempt_id", "outcome", "billable",
                "usage_verified", "actual", "queue_latency_ms", "run_latency_ms",
            }
            allowed = required_settlement | {"price_source"}
            if set(settlement).difference(allowed) or not required_settlement.issubset(settlement):
                raise ValueError("attempt settlement schema is invalid")
            if settlement["outcome"] not in {"success", "failed", "cancelled"} or type(settlement["billable"]) is not bool or type(settlement["usage_verified"]) is not bool:
                raise ValueError("attempt settlement state is invalid")
            if settlement["usage_verified"]:
                if not isinstance(settlement["actual"], dict):
                    raise ValueError("verified usage is missing")
                usage = settlement["actual"]
            elif settlement["actual"] is not None:
                raise ValueError("unverified usage must retain the reservation")
            outcome = settlement["outcome"]
            billable = settlement["billable"]
            queue_latency = settlement["queue_latency_ms"]
            run_latency = settlement["run_latency_ms"]
            if any(type(value) is not int or value < 0 for value in (queue_latency, run_latency)):
                raise ValueError("attempt latency is invalid")
            known_price = (
                settlement.get("price_source") is not None
                and isinstance(settlement.get("price_source"), str)
                and bool(settlement.get("price_source"))
                and "currency_micros" in usage
            )
        allowed_usage = {"input_tokens", "output_tokens", "cache_tokens", "provider_usage_units", "currency_micros"}
        if set(usage).difference(allowed_usage) or any(type(value) is not int or value < 0 for value in usage.values()):
            raise ValueError("attempt usage is invalid")
        intent = start["intent"]
        realized = per_intent.setdefault(intent, _empty_cost())
        stage = stage_costs.setdefault((intent, start["stage"]), _empty_cost())
        currency_known.setdefault(intent, True)
        candidate_stages.setdefault((intent, start["candidate_id"]), set()).add(start["stage"])
        unit = _empty_cost()
        unit["calls"] = 1
        unit["failed_calls"] = int(outcome == "failed")
        unit["billable_cancelled_calls"] = int(outcome == "cancelled" and billable)
        kind_counter = start["attempt_kind"] + "_calls"
        if kind_counter in unit:
            unit[kind_counter] = 1
        for field in ("input_tokens", "output_tokens", "cache_tokens", "provider_usage_units"):
            unit[field] = usage.get(field, 0)
        unit["queue_latency_ms"] = queue_latency
        unit["run_latency_ms"] = run_latency
        if known_price:
            unit["currency_micros"] = usage["currency_micros"]
            realized.setdefault("currency_micros", 0)
            stage.setdefault("currency_micros", 0)
        elif billable:
            currency_known[intent] = False
        for target in (realized, stage):
            for field, value in unit.items():
                target[field] = target.get(field, 0) + value
    result = {"intents": {}}
    for intent, realized in sorted(per_intent.items()):
        candidates_for_intent = [key for key in candidate_stages if key[0] == intent]
        candidate_count = len(candidates_for_intent)
        escalated = sum("l2" in candidate_stages[key] for key in candidates_for_intent)
        rate = escalated / candidate_count if candidate_count else 0.0
        l1 = stage_costs.get((intent, "l1"), _empty_cost())
        l2 = stage_costs.get((intent, "l2"), _empty_cost())
        expected = _empty_cost()
        l1_per_candidate = {field: value / candidate_count for field, value in l1.items()} if candidate_count else _empty_cost()
        l2_per_escalation = {field: value / escalated for field, value in l2.items()} if escalated else _empty_cost()
        _add_cost(expected, l1_per_candidate)
        _add_cost(expected, l2_per_escalation, rate)
        expected["formula"] = "L1 + escalation_rate * L2"
        if not currency_known[intent]:
            realized.pop("currency_micros", None)
            l1.pop("currency_micros", None)
            l2.pop("currency_micros", None)
            expected.pop("currency_micros", None)
        result["intents"][intent] = {
            "candidate_count": candidate_count,
            "escalated_candidate_count": escalated,
            "escalation_rate": rate,
            "realized": realized,
            "l1_realized": l1,
            "l2_realized": l2,
            "expected_per_candidate": expected,
        }
    return result
