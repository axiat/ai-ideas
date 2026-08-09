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
        "lineage_relation",
        "historical_text", "evidence_anchors", "adjudication_state",
        "negative_kind", "risk_slices", "partition", "scope",
    }
)
_LINEAGE_RELATIONS = frozenset(
    {"same_revision", "evolved_from", "recheck_of", "supersedes", "none"}
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
        "semantic_policy", "plan", "prompt", "schema", "ordered_provider_pools",
        "capacity", "provider", "fault", "replay",
    }
)
_ONE_SIDED_95_Z = 1.6448536269514722
_CRITICAL_SLICES = frozenset(
    {"low_overlap", "cross_language", "lineage_revision"}
)
RISK_SLICE_POLICY_V1 = {
    "schema_version": "history-risk-slice-policy-v1",
    "policy_version": "critical-semantic-slices-v1",
    "allowed_slices": sorted(_CRITICAL_SLICES),
}
RISK_POLICY_V1_RULES = [
    {
        "rule_id": "retriever_uncalibrated",
        "fact": "retriever_calibrated",
        "equals": False,
        "required_route": "exhaustive",
        "pre_l1": True,
    },
    {
        "rule_id": "finalist_or_sa",
        "fact": "finalist_or_sa",
        "equals": True,
        "required_route": "guarded",
        "pre_l1": False,
    },
    {
        "rule_id": "mandatory_channel_failed",
        "fact": "mandatory_channel_failed",
        "equals": True,
        "required_route": "exhaustive",
        "pre_l1": True,
    },
    {
        "rule_id": "comparator_uncertain",
        "fact": "comparator_uncertain",
        "equals": True,
        "required_route": "guarded",
        "pre_l1": False,
    },
    {
        "rule_id": "bad_slice_membership",
        "fact": "bad_slice_membership",
        "equals": True,
        "required_route": "guarded",
        "pre_l1": False,
    },
    {
        "rule_id": "index_profile_recently_changed",
        "fact": "index_profile_recently_changed",
        "equals": True,
        "required_route": "exhaustive",
        "pre_l1": True,
    },
    {
        "rule_id": "permanent_no_match_without_release_gate",
        "fact": "permanent_no_match_without_release_gate",
        "equals": True,
        "required_route": "exhaustive",
        "pre_l1": True,
    },
]


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


def semantic_policy_authority(policy):
    """Validate and return the evaluator's one canonical policy identity."""
    normalized = _validate_policy(policy)
    canonical = contract.canonical_bytes(_decimal_identity(normalized))
    return {
        "policy": normalized,
        "canonical_bytes": canonical,
        "sha256": contract.framed_sha256(
            "semantic-release-policy-v1", canonical
        ),
    }


def semantic_policy_sha256(policy):
    """Return the exact fixed-decimal identity of a validated release policy."""
    return semantic_policy_authority(policy)["sha256"]


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
    critical_slices = shadow["critical_slices"]
    if (
        not isinstance(critical_slices, dict)
        or not critical_slices
        or any(
            not isinstance(name, str) or not name
            or type(minimum) is not int or minimum < 0
            for name, minimum in critical_slices.items()
        )
    ):
        raise ValueError("critical slice thresholds are invalid")
    if (
        shadow["minimum_positive_lineages"] < 30
        or shadow["minimum_negative_lineages"] < 20
        or not _CRITICAL_SLICES.issubset(critical_slices)
        or any(critical_slices[name] < 5 for name in _CRITICAL_SLICES)
    ):
        raise ValueError("shadow policy lowers a contractual minimum")
    production = policy["production"]
    if not isinstance(production, dict) or set(production) != {
        "minimum_positive_lineages", "aggregate", "required_slices"
    }:
        raise ValueError("production policy fields are invalid")
    if type(production["minimum_positive_lineages"]) is not int or production["minimum_positive_lineages"] < 0:
        raise ValueError("production positive threshold is invalid")
    required_slices = production["required_slices"]
    aggregate = production["aggregate"]
    if (
        not isinstance(required_slices, dict)
        or not isinstance(aggregate, dict)
    ):
        raise ValueError("production policy containers are invalid")
    if (
        production["minimum_positive_lineages"] < 300
        or not _CRITICAL_SLICES.issubset(required_slices)
    ):
        raise ValueError("production policy lowers a contractual minimum")
    for metric_policy in [aggregate] + list(required_slices.values()):
        if not isinstance(metric_policy, dict) or set(metric_policy) != {
            "minimum_observations", "minimum_recall_lower_bound",
            "maximum_false_negative_upper_bound",
        }:
            raise ValueError("production metric gate is invalid")
        if type(metric_policy["minimum_observations"]) is not int or metric_policy["minimum_observations"] < 1:
            raise ValueError("production observation threshold is invalid")
        for name in ("minimum_recall_lower_bound", "maximum_false_negative_upper_bound"):
            value = metric_policy[name]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or value < 0
                or value > 1
            ):
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
            or any(
                type(value) is not str or not value
                for value in values
            )
        ):
            raise ValueError("qrel partition lineages are invalid")
        seen_values = set()
        for value in values:
            if value in seen_values:
                raise ValueError("qrel partition lineages are invalid")
            seen_values.add(value)
        for lineage_id in values:
            if lineage_id in partition_lineages:
                raise ValueError("query lineage leaks across partitions")
            partition_lineages[lineage_id] = partition
    normalized = []
    qrel_ids = set()
    judgments = set()
    lineage_partition = {}
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
            if type(row[field]) is not str or not row[field]:
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
        if (
            type(row["semantic_relation"]) is not str
            or row["semantic_relation"] not in SEMANTIC_RELATIONS
        ):
            raise ValueError("qrel semantic relation is invalid")
        if (
            type(row["lineage_relation"]) is not str
            or row["lineage_relation"] not in _LINEAGE_RELATIONS
        ):
            raise ValueError("qrel lineage relation is invalid")
        if (
            type(row["adjudication_state"]) is not str
            or row["adjudication_state"] not in {"adjudicated", "pending"}
        ):
            raise ValueError("qrel adjudication state is invalid")
        if (
            row["negative_kind"] is not None
            and (
                type(row["negative_kind"]) is not str
                or row["negative_kind"]
                not in {"hard_negative", "true_no_match"}
            )
        ):
            raise ValueError("qrel negative kind is invalid")
        if row["semantic_relation"] == "blocking_duplicate" and row["negative_kind"] is not None:
            raise ValueError("positive qrel cannot be a negative")
        if row["negative_kind"] is not None and row["semantic_relation"] != "distinct":
            raise ValueError("negative qrel must use the shared distinct relation")
        anchors = row["evidence_anchors"]
        if (
            not isinstance(anchors, list)
            or not anchors
            or any(
                type(anchor) is not str
                or not anchor
                or anchor not in row["historical_text"]
                for anchor in anchors
            )
        ):
            raise ValueError("qrel evidence anchors are not extractive")
        seen_anchors = set()
        for anchor in anchors:
            if anchor in seen_anchors:
                raise ValueError("qrel evidence anchors are not extractive")
            seen_anchors.add(anchor)
        slices = row["risk_slices"]
        if (
            not isinstance(slices, list)
            or any(type(value) is not str or not value for value in slices)
        ):
            raise ValueError("qrel risk slices are invalid")
        seen_slices = set()
        for value in slices:
            if value in seen_slices:
                raise ValueError("qrel risk slices are invalid")
            seen_slices.add(value)
        if slices != sorted(slices):
            raise ValueError("qrel risk slices are invalid")
        partition = row["partition"]
        if partition not in _PARTITIONS or partition_lineages.get(row["query_lineage_id"]) != partition:
            raise ValueError("qrel partition membership is inconsistent")
        for mapping, identity, message in (
            (lineage_partition, row["query_lineage_id"], "lineage leaks across partitions or roles"),
            (lineage_partition, row["historical_lineage_id"], "lineage leaks across partitions or roles"),
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
    replayed = validate_qrels(
        dataset["rows"], dataset["partitions"], scope=dataset["scope"]
    )
    if contract.canonical_bytes(dataset) != contract.canonical_bytes(replayed):
        raise ValueError("validated qrels are not canonical")
    return replayed


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
        if (
            type(row["query_id"]) is not str
            or not row["query_id"]
            or type(row["historical_id"]) is not str
            or not row["historical_id"]
        ):
            raise ValueError("evaluation output identity is invalid")
        pair = (row["query_id"], row["historical_id"])
        semantic_relation = row["semantic_relation"]
        if (
            pair in observed
            or type(semantic_relation) is not str
            or semantic_relation not in SEMANTIC_RELATIONS
        ):
            raise ValueError("evaluation output is duplicated or invalid")
        observed[pair] = semantic_relation
    if set(observed) != expected:
        raise ValueError("evaluation outputs require exact query coverage")
    return observed


def _lineage_outcomes(dataset, output_by_query, *, partition=None):
    positive = {}
    negative = set()
    slices = {}
    for row in dataset["rows"]:
        if partition is not None and row["partition"] != partition:
            continue
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


def _production_metrics(dataset, output_by_query, normalized_policy):
    positive, negative, slices = _lineage_outcomes(
        dataset, output_by_query, partition="test"
    )
    z = normalized_policy["wilson_one_sided_z"]
    aggregate = _metric(sum(positive.values()), len(positive), z)
    aggregate["state"] = _apply_metric_gate(
        aggregate, normalized_policy["production"]["aggregate"]
    )
    slice_metrics = {}
    vetoes = []
    for name, gate in sorted(
        normalized_policy["production"]["required_slices"].items()
    ):
        members = [
            lineage for lineage in positive
            if name in slices.get(lineage, set())
        ]
        metric = _metric(
            sum(1 for lineage in members if positive[lineage]),
            len(members), z,
        )
        metric["state"] = _apply_metric_gate(metric, gate)
        slice_metrics[name] = metric
        if metric["state"] != "passed":
            vetoes.append("slice_%s_%s" % (name, metric["state"]))
    if len(positive) < normalized_policy["production"][
        "minimum_positive_lineages"
    ]:
        vetoes.append("insufficient_positive_lineages")
    if aggregate["state"] != "passed":
        vetoes.append("aggregate_%s" % aggregate["state"])
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
    return metrics, vetoes


def semantic_evaluation_identities(qrels, outputs, policy):
    """Rebuild exact qrels, normalized-output, and computed-metric identities."""
    dataset = _validate_dataset(qrels)
    normalized_policy = _validate_policy(policy)
    output_by_query = _validate_outputs(dataset, outputs)
    metrics, _ = _production_metrics(
        dataset, output_by_query, normalized_policy
    )
    normalized_outputs = [
        {
            "query_id": query_id,
            "historical_id": historical_id,
            "semantic_relation": output_by_query[(query_id, historical_id)],
        }
        for query_id, historical_id in sorted(output_by_query)
    ]
    evaluation_material = {
        "schema_version": "history-semantic-evaluation-v1",
        "qrels_hash": dataset["qrels_hash"],
        "outputs": normalized_outputs,
    }
    evaluation_hash = _hash(
        "history-semantic-evaluation-v1", evaluation_material
    )
    policy_sha = _policy_hash(normalized_policy)
    metric_material = {
        "schema_version": "history-semantic-metric-report-v1",
        "qrels_hash": dataset["qrels_hash"],
        "evaluation_hash": evaluation_hash,
        "policy_sha256": policy_sha,
        "metrics": _decimal_identity(metrics),
    }
    return {
        "qrels_hash": dataset["qrels_hash"],
        "evaluation_hash": evaluation_hash,
        "metric_report_hash": _hash(
            "history-semantic-metric-report-v1", metric_material
        ),
        "policy_sha256": policy_sha,
        "metrics": metrics,
    }


def evaluate_production_qualification(qrels, outputs, policy, evidence):
    """Produce an immutable qualification candidate or an exact veto list."""
    dataset = _validate_dataset(qrels)
    normalized_policy = _validate_policy(policy)
    policy_sha = _policy_hash(normalized_policy)
    normalized_evidence = _validate_evidence(evidence, policy_sha)
    expires_at = _timestamp(
        normalized_evidence["expires_at"], "expires_at"
    )
    if expires_at <= datetime.datetime.now(datetime.timezone.utc):
        raise ValueError("production evidence is expired")
    output_by_query = _validate_outputs(dataset, outputs)
    metrics, vetoes = _production_metrics(
        dataset, output_by_query, normalized_policy
    )
    identities = semantic_evaluation_identities(
        dataset, outputs, normalized_policy
    )
    if (
        normalized_evidence["evaluation_hash"]
        != identities["evaluation_hash"]
        or normalized_evidence["metric_report_hash"]
        != identities["metric_report_hash"]
    ):
        raise ValueError("production evaluation identity is invalid")
    if dataset["scope"] not in {"real", "production", "real_qrels"}:
        vetoes.append("non_production_scope")
    if not normalized_evidence["provider_capacity_complete"]:
        vetoes.append("provider_capacity_incomplete")
    if not normalized_evidence["fault_evidence_passed"]:
        vetoes.append("fault_evidence_failed")
    if not normalized_evidence["replay_evidence_passed"]:
        vetoes.append("replay_evidence_failed")
    vetoes = sorted(set(vetoes))
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
    if (
        ordered_rules["risk_policy_version"] != "risk-policy-v1"
        or contract.canonical_bytes(rules)
        != contract.canonical_bytes(RISK_POLICY_V1_RULES)
    ):
        raise ValueError("risk policy is not the host-authorized v1 policy")
    return copy.deepcopy(rules)


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


def _per_unit_cost(total, denominator, *, latency_complete, currency_complete):
    if denominator <= 0:
        normalized = {field: 0.0 for field in _empty_cost()}
    else:
        normalized = {
            field: total.get(field, 0) / denominator
            for field in _empty_cost()
        }
    if not latency_complete:
        normalized.pop("queue_latency_ms", None)
        normalized.pop("run_latency_ms", None)
    if currency_complete and "currency_micros" in total:
        normalized["currency_micros"] = (
            total["currency_micros"] / denominator
            if denominator > 0 else 0.0
        )
    return normalized


def _derived_attempt_kind_availability(conn, run_id, intent):
    plans = conn.execute(
        """
        SELECT plan.plan_sha, plan.plan_json,
               generation.generation_id, generation.material_json
        FROM audit_l2_plans_v2 plan
        LEFT JOIN audit_l2_adjudication_generations_v2 generation
          ON generation.plan_sha=plan.plan_sha
        WHERE plan.run_id=? AND plan.intent=?
        ORDER BY plan.plan_sha
        """,
        (run_id, intent),
    ).fetchall()
    unavailable = {"detail": "producer_unavailable", "reduce": "producer_unavailable"}
    if not plans or any(row["generation_id"] is None for row in plans):
        return unavailable
    expected = {"detail": 0, "reduce": 0}
    try:
        for row in plans:
            plan = contract.parse_json_bytes(
                (row["plan_json"] + "\n").encode("utf-8")
            )
            generation = contract.parse_json_bytes(
                (row["material_json"] + "\n").encode("utf-8")
            )
            exceptional_count = sum(
                len(card["item_ids"])
                for card in generation["exceptional_cards"]
            )
            item_cap = plan["capacity_profile"]["item_cap"]
            if type(item_cap) is not int or item_cap <= 0:
                raise ValueError
            expected["detail"] += exceptional_count
            if exceptional_count:
                expected["reduce"] += (
                    exceptional_count + item_cap - 1
                ) // item_cap
    except (KeyError, TypeError, ValueError, contract.ContractV2Error) as exc:
        raise ValueError("adjudication cost authority is invalid") from exc
    if expected["detail"] == 0:
        return {"detail": "not_required", "reduce": "not_required"}
    task_rows = conn.execute(
        """
        SELECT authority.stage, authority.task_hash,
               CASE WHEN valid.task_hash IS NULL THEN 0 ELSE 1 END AS valid,
               count(attempt.attempt_id) AS attempt_count,
               sum(
                 CASE WHEN attempt.attempt_id IS NOT NULL
                       AND launch.attempt_id IS NOT NULL
                       AND terminal.attempt_id IS NOT NULL
                      THEN 1 ELSE 0 END
               ) AS durable_attempt_count
        FROM audit_l2_derived_task_authority_v2 authority
        JOIN audit_l2_adjudication_generations_v2 generation
          ON generation.generation_id=authority.generation_id
        JOIN audit_l2_plans_v2 plan ON plan.plan_sha=generation.plan_sha
        LEFT JOIN audit_l2_valid_adjudication_task_authority_v2 valid
          ON valid.task_hash=authority.task_hash
        LEFT JOIN audit_task_attempts attempt
          ON attempt.task_hash=authority.task_hash
        LEFT JOIN audit_attempt_launch_facts_v2 launch
          ON launch.attempt_id=attempt.attempt_id
        LEFT JOIN audit_attempt_cost_settlements_v2 terminal
          ON terminal.attempt_id=attempt.attempt_id
        WHERE plan.run_id=? AND plan.intent=?
        GROUP BY authority.stage, authority.task_hash, valid.task_hash
        ORDER BY authority.stage, authority.task_hash
        """,
        (run_id, intent),
    ).fetchall()
    result = {}
    for stage in ("detail", "reduce"):
        stage_rows = [row for row in task_rows if row["stage"] == stage]
        durable = (
            len(stage_rows) == expected[stage]
            and all(row["valid"] == 1 for row in stage_rows)
            and all(row["attempt_count"] > 0 for row in stage_rows)
            and all(
                row["durable_attempt_count"] == row["attempt_count"]
                for row in stage_rows
            )
        )
        result[stage] = "durable" if durable else "pending"
    return result


def _validate_intent_route_authority(run_id, cohort, route_rows):
    """Validate one intent cohort without sharing completeness across intents."""
    observation_complete = True
    source_complete = True
    try:
        candidate_ids = contract.parse_json_bytes(
            (cohort["candidate_ids_json"] + "\n").encode("utf-8")
        )
        risk_policy = contract.parse_json_bytes(
            (cohort["risk_policy_json"] + "\n").encode("utf-8")
        )
        slice_policy = contract.parse_json_bytes(
            (cohort["risk_slice_policy_json"] + "\n").encode("utf-8")
        )
    except (TypeError, ValueError, contract.ContractV2Error) as exc:
        raise ValueError("candidate route cohort is not canonical") from exc
    risk_policy_sha = _hash("history-risk-policy-v1", risk_policy)
    slice_policy_sha = _hash("history-risk-slice-policy-v1", slice_policy)
    cohort_material = {
        "run_id": run_id, "batch_id": cohort["batch_id"],
        "intent": cohort["intent"], "candidate_ids": candidate_ids,
        "risk_policy_sha256": risk_policy_sha,
        "risk_slice_policy_sha256": slice_policy_sha,
        "created_at": cohort["created_at"],
    }
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or candidate_ids != sorted(candidate_ids)
        or len(set(candidate_ids)) != len(candidate_ids)
        or risk_policy_sha != cohort["risk_policy_sha256"]
        or slice_policy != RISK_SLICE_POLICY_V1
        or slice_policy_sha != cohort["risk_slice_policy_sha256"]
        or _hash("history-candidate-route-cohort-v2", cohort_material)
            != cohort["cohort_sha256"]
        or [row["candidate_id"] for row in route_rows] != candidate_ids
        or any(row["intent"] != cohort["intent"] for row in route_rows)
    ):
        raise ValueError("candidate route cohort authority is inconsistent")
    for route in route_rows:
        try:
            router_facts = contract.parse_json_bytes(
                (route["router_facts_json"] + "\n").encode("utf-8")
            )
            risk_slices = contract.parse_json_bytes(
                (route["risk_slices_json"] + "\n").encode("utf-8")
            )
            matched_rule_ids = contract.parse_json_bytes(
                (route["matched_rule_ids_json"] + "\n").encode("utf-8")
            )
            replay = route_candidate(router_facts, risk_policy)
        except (TypeError, ValueError, contract.ContractV2Error) as exc:
            raise ValueError("candidate route fact is not replayable") from exc
        route_material = {
            "run_id": run_id, "candidate_id": route["candidate_id"],
            "intent": route["intent"],
            "cohort_sha256": cohort["cohort_sha256"],
            "router_facts": router_facts, "risk_slices": risk_slices,
            "matched_rule_ids": matched_rule_ids,
            "route": replay["route"],
            "call_l1_model": replay["call_l1_model"],
            "dispatch_allowed": replay["dispatch_allowed"],
            "rule_table_sha256": replay["rule_table_sha256"],
            "risk_policy_version": replay["receipt_risk_policy_version"],
            "created_at": route["created_at"],
        }
        if (
            not isinstance(risk_slices, list)
            or any(value not in _CRITICAL_SLICES for value in risk_slices)
            or bool(risk_slices) != router_facts["bad_slice_membership"]
            or matched_rule_ids != replay["matched_rule_ids"]
            or route["route"] != replay["route"]
            or route["call_l1_model"] != int(replay["call_l1_model"])
            or route["dispatch_allowed"] != int(replay["dispatch_allowed"])
            or route["rule_table_sha256"] != replay["rule_table_sha256"]
            or route["risk_policy_version"]
                != replay["receipt_risk_policy_version"]
            or _hash("history-candidate-route-fact-v2", route_material)
                != route["fact_sha256"]
        ):
            raise ValueError("candidate route fact authority is inconsistent")
        if (
            route["legacy_route_fact_sha256"] is not None
            or route["bound_route_fact_sha256"] != route["fact_sha256"]
            or route["bound_final_phase_fact_sha256"]
                != route["exact_final_phase_fact_sha256"]
            or route["bound_source_set_sha256"]
                != route["exact_final_source_set_sha256"]
            or route["exact_source_round_sha256"] is None
            or route["router_facts_json"] != route["final_router_facts_json"]
            or route["risk_slices_json"] != route["final_risk_slices_json"]
            or route["matched_rule_ids_json"]
                != route["final_matched_rule_ids_json"]
            or route["route"] != route["final_route"]
            or route["call_l1_model"] != route["final_call_l1_model"]
            or route["dispatch_allowed"] != route["final_dispatch_allowed"]
            or route["rule_table_sha256"] != route["final_rule_table_sha256"]
            or route["risk_policy_version"]
                != route["final_risk_policy_version"]
        ):
            source_complete = False
        if route["boundary_sha256"] is None:
            observation_complete = False
        else:
            observation_material = {
                "run_id": run_id,
                "candidate_id": route["candidate_id"],
                "route_fact_sha256": route["fact_sha256"],
                "observation_scope": "host_issued_shadow",
                "production_authority": False,
                "created_at": route["observation_created_at"],
            }
            if (
                route["observation_scope"] != "host_issued_shadow"
                or route["production_authority"] != 0
                or _hash(
                    "history-candidate-route-observation-boundary-v1",
                    observation_material,
                ) != route["boundary_sha256"]
            ):
                raise ValueError(
                    "candidate route observation boundary is inconsistent"
                )
        if route["actual_l2_dispatch"]:
            dispatch_material = {
                "plan_sha": route["dispatch_plan_sha"], "run_id": run_id,
                "candidate_id": route["candidate_id"],
                "route_fact_sha256": route["fact_sha256"],
                "created_at": route["dispatch_created_at"],
            }
            if (
                not replay["dispatch_allowed"]
                or route["dispatch_plan_valid"] != 1
                or _hash(
                    "history-candidate-l2-dispatch-v2", dispatch_material
                ) != route["dispatch_sha256"]
            ):
                raise ValueError(
                    "candidate route dispatch authority is inconsistent"
                )
    return observation_complete and source_complete


def _route_authority_completeness_by_intent(run_id, cohorts, route_rows):
    cohort_by_intent = {}
    for cohort in cohorts:
        intent = cohort["intent"]
        if intent in cohort_by_intent:
            raise ValueError("candidate route cohort intent is ambiguous")
        cohort_by_intent[intent] = cohort
    route_rows_by_intent = {}
    for route in route_rows:
        route_rows_by_intent.setdefault(route["intent"], []).append(route)
    if set(route_rows_by_intent).difference(cohort_by_intent):
        raise ValueError("candidate route cohort authority is inconsistent")
    return {
        intent: _validate_intent_route_authority(
            run_id, cohort, route_rows_by_intent.get(intent, [])
        )
        for intent, cohort in cohort_by_intent.items()
    }


def summarize_realized_cost(conn, run_id):
    """Summarize durable Task5 attempt cost facts for one exact run."""
    import sqlite3
    try:
        from lib import history_audit_store
    except ImportError:
        import history_audit_store
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3 connection")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id is required")
    rows = conn.execute(
        """
        SELECT reservation.attempt_id, reservation.intent,
               reservation.candidate_id, reservation.attempt_kind,
               reservation.reserved_json, task.stage, task.task_hash,
               attempt.ordinal, attempt.provenance_json,
               binding.parent_task_hash, binding.provider_pool_json,
               budget.usage_verified, budget.actual_json,
               budget.created_at AS budget_created_at,
               launch.queue_latency_ms,
               terminal.outcome, terminal.billing_state,
               terminal.usage_source, terminal.price_source,
               terminal.currency, terminal.run_latency_ms,
               terminal.completed_at AS terminal_completed_at,
               completion.attempt_id AS completion_id,
               completion.outcome AS completion_outcome,
               completion.output_cas_object_id AS completion_output_id,
               completion.completed_at AS completion_completed_at
        FROM audit_runtime_budget_reservations_v2 reservation
        JOIN audit_l2_plans_v2 plan ON plan.plan_sha=reservation.plan_sha
        JOIN audit_task_attempts attempt
          ON attempt.attempt_id=reservation.attempt_id
         AND attempt.task_hash=reservation.task_hash
        JOIN audit_logical_tasks task ON task.task_hash=attempt.task_hash
        JOIN audit_task_bindings_v2 binding ON binding.task_hash=task.task_hash
        JOIN audit_attempt_launch_facts_v2 launch
          ON launch.attempt_id=attempt.attempt_id
        LEFT JOIN audit_runtime_budget_settlements_v2 budget
          ON budget.attempt_id=attempt.attempt_id
        LEFT JOIN audit_attempt_completions_v2 completion
          ON completion.attempt_id=attempt.attempt_id
        LEFT JOIN audit_attempt_cost_settlements_v2 terminal
          ON terminal.attempt_id=attempt.attempt_id
        WHERE plan.run_id=?
        ORDER BY task.task_hash, attempt.ordinal
        """,
        (run_id,),
    ).fetchall()
    reservation_count = conn.execute(
        """
        SELECT count(*) FROM audit_runtime_budget_reservations_v2 reservation
        JOIN audit_l2_plans_v2 plan ON plan.plan_sha=reservation.plan_sha
        WHERE plan.run_id=?
        """,
        (run_id,),
    ).fetchone()[0]
    if reservation_count != len(rows):
        raise ValueError("durable cost launch parity is incomplete")
    cohorts = conn.execute(
        "SELECT * FROM audit_candidate_route_cohorts_v2 WHERE run_id=?",
        (run_id,),
    ).fetchall()
    route_rows = conn.execute(
        """
        SELECT route.*,
               observation.observation_scope,
               observation.production_authority,
               observation.boundary_sha256,
               observation.created_at AS observation_created_at,
               source_binding.route_fact_sha256
                 AS bound_route_fact_sha256,
               source_binding.final_phase_fact_sha256
                 AS bound_final_phase_fact_sha256,
               source_binding.source_set_sha256
                 AS bound_source_set_sha256,
               final_phase.phase_fact_sha256
                 AS exact_final_phase_fact_sha256,
               final_phase.router_facts_json AS final_router_facts_json,
               final_phase.risk_slices_json AS final_risk_slices_json,
               final_phase.matched_rule_ids_json
                 AS final_matched_rule_ids_json,
               final_phase.route AS final_route,
               final_phase.call_l1_model AS final_call_l1_model,
               final_phase.dispatch_allowed AS final_dispatch_allowed,
               final_phase.rule_table_sha256 AS final_rule_table_sha256,
               final_phase.risk_policy_version
                 AS final_risk_policy_version,
               source_set.source_set_sha256
                 AS exact_final_source_set_sha256,
               source_round.route_round_sha256
                 AS exact_source_round_sha256,
               legacy.route_fact_sha256 AS legacy_route_fact_sha256,
               CASE WHEN dispatch.plan_sha IS NULL THEN 0 ELSE 1 END
                 AS actual_l2_dispatch,
               dispatch.plan_sha AS dispatch_plan_sha,
               dispatch.dispatch_sha256, dispatch.created_at AS dispatch_created_at,
               CASE WHEN dispatch.plan_sha IS NULL THEN 1
                    WHEN plan.plan_sha IS NOT NULL THEN 1 ELSE 0 END
                 AS dispatch_plan_valid
        FROM audit_candidate_route_facts_v2 route
        LEFT JOIN audit_candidate_route_observation_boundaries_v2 observation
          ON observation.run_id=route.run_id
         AND observation.candidate_id=route.candidate_id
         AND observation.route_fact_sha256=route.fact_sha256
        LEFT JOIN audit_candidate_route_source_bindings_v2 source_binding
          ON source_binding.run_id=route.run_id
         AND source_binding.candidate_id=route.candidate_id
         AND source_binding.route_fact_sha256=route.fact_sha256
        LEFT JOIN audit_router_phase_facts_v2 final_phase
          ON final_phase.phase_fact_sha256=
               source_binding.final_phase_fact_sha256
         AND final_phase.phase='final'
         AND final_phase.candidate_id=route.candidate_id
         AND final_phase.source_set_sha256=source_binding.source_set_sha256
        LEFT JOIN audit_router_source_sets_v2 source_set
          ON source_set.source_set_sha256=source_binding.source_set_sha256
         AND source_set.phase='final'
         AND source_set.route_round_sha256=final_phase.route_round_sha256
        LEFT JOIN audit_router_rounds_v2 source_round
          ON source_round.route_round_sha256=final_phase.route_round_sha256
         AND source_round.run_id=route.run_id
         AND source_round.intent=route.intent
        LEFT JOIN audit_legacy_candidate_route_authorities_v2 legacy
          ON legacy.route_fact_sha256=route.fact_sha256
          OR (legacy.run_id=route.run_id
              AND legacy.candidate_id=route.candidate_id)
        LEFT JOIN audit_candidate_l2_dispatch_facts_v2 dispatch
         ON dispatch.run_id=route.run_id
         AND dispatch.candidate_id=route.candidate_id
         AND dispatch.route_fact_sha256=route.fact_sha256
        LEFT JOIN audit_l2_plans_v2 plan
          ON plan.plan_sha=dispatch.plan_sha
         AND plan.run_id=dispatch.run_id
         AND plan.candidate_id=dispatch.candidate_id
         AND plan.intent=route.intent
        WHERE route.run_id=? ORDER BY route.intent, route.candidate_id
        """,
        (run_id,),
    ).fetchall()
    route_facts_complete_by_intent = (
        _route_authority_completeness_by_intent(run_id, cohorts, route_rows)
    )
    l1_fact_rows = []
    has_l1_facts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='audit_l1_attempt_facts_v2'"
    ).fetchone() is not None
    if has_l1_facts:
        raw_l1_rows = conn.execute(
            "SELECT * FROM audit_l1_attempt_facts_v2 WHERE run_id=? "
            "ORDER BY intent,candidate_id,ordinal", (run_id,),
        ).fetchall()
        l1_fact_rows = conn.execute(
            "SELECT * FROM audit_l1_valid_attempt_facts_v2 WHERE run_id=? "
            "ORDER BY intent,candidate_id,ordinal", (run_id,),
        ).fetchall()
        if (
            len(raw_l1_rows) != len(l1_fact_rows)
            or [row["attempt_id"] for row in raw_l1_rows]
                != [row["attempt_id"] for row in l1_fact_rows]
            or any(
                history_audit_store._l1_attempt_fact_row_valid(*tuple(row))
                != 1
                for row in raw_l1_rows
            )
        ):
            raise ValueError("durable L1 attempt authority is inconsistent")
    l1_costs = {}
    l1_candidates = {}
    l1_currency_complete = {}
    l1_latency_complete = {}
    l1_prior_attempt = {}
    for row in l1_fact_rows:
        try:
            reserved = json.loads(row["reserved_json"])
            actual = (
                None if row["actual_json"] is None
                else json.loads(row["actual_json"])
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("durable L1 usage is invalid") from exc
        usage = actual if row["usage_source"] == "verified_actual" else reserved
        intent = row["intent"]
        target = l1_costs.setdefault(intent, _empty_cost())
        l1_candidates.setdefault(intent, set()).add(row["candidate_id"])
        l1_currency_complete.setdefault(intent, True)
        l1_latency_complete.setdefault(intent, True)
        target["calls"] += 1
        prior_key = (intent, row["candidate_id"])
        if row["ordinal"] > 0:
            previous = l1_prior_attempt.get(prior_key)
            if (
                previous is None
                or previous["ordinal"] != row["ordinal"] - 1
            ):
                raise ValueError("durable L1 attempt sequence is incomplete")
            counter = (
                "retry_calls"
                if previous["provider"] == row["provider"]
                else "failover_calls"
            )
            target[counter] += 1
        if row["outcome"] == "failed":
            target["failed_calls"] += 1
        if row["outcome"] == "cancelled" and row["billing_state"] == "billable":
            target["billable_cancelled_calls"] += 1
        for field in (
            "input_tokens", "output_tokens", "cache_tokens",
            "provider_usage_units",
        ):
            target[field] += usage.get(field, 0)
        target["queue_latency_ms"] += row["queue_latency_ms"]
        target["run_latency_ms"] += row["run_latency_ms"]
        if row["billing_state"] == "billable":
            target.setdefault("currency_micros", 0)
            target["currency_micros"] += usage["currency_micros"]
        elif row["billing_state"] == "unknown":
            l1_currency_complete[intent] = False
        target["unverified_usage_calls"] = target.get(
            "unverified_usage_calls", 0
        ) + int(row["usage_source"] == "reservation")
        l1_prior_attempt[prior_key] = row
    candidate_rows = route_rows or conn.execute(
        "SELECT intent, candidate_id FROM audit_l2_plans_v2 "
        "WHERE run_id=? ORDER BY candidate_id", (run_id,),
    ).fetchall()
    per_intent = {}
    candidates = {}
    route_by_candidate = {}
    for candidate in candidate_rows:
        per_intent.setdefault(candidate["intent"], _empty_cost())
        candidates.setdefault(candidate["intent"], set()).add(
            candidate["candidate_id"]
        )
        if route_facts_complete_by_intent.get(candidate["intent"], False):
            route_by_candidate[(candidate["intent"], candidate["candidate_id"])] = candidate
    latency_complete = {}
    currency_complete = {}
    provider_groups = {}
    slice_groups = {}
    group_completeness = {}
    prior_by_task = {}
    for row in rows:
        if (
            row["completion_id"] is not None and row["outcome"] is None
        ) or (
            row["completion_id"] is None
            and row["outcome"] not in (None, "cancelled")
        ):
            raise ValueError("durable cost terminal parity is incomplete")
        provenance = json.loads(row["provenance_json"])
        if contract.attempt_id(
            row["task_hash"], row["ordinal"], provenance
        ) != row["attempt_id"]:
            raise ValueError("durable cost attempt identity is inconsistent")
        if row["ordinal"] == 0:
            derived_kind = (
                row["stage"] if row["stage"] in {"detail", "reduce"}
                else "split" if row["parent_task_hash"] is not None
                else "initial"
            )
        else:
            prior = prior_by_task.get(row["task_hash"])
            if prior is None or prior["ordinal"] != row["ordinal"] - 1:
                raise ValueError("durable cost attempt sequence is incomplete")
            if prior["outcome"] in {"timeout", "429", "5xx"}:
                derived_kind = "failover"
            elif prior["outcome"] in {"syntax", "schema", "cancelled"}:
                derived_kind = "retry"
            else:
                raise ValueError("durable cost prior outcome is not retryable")
        if (
            row["attempt_kind"] != derived_kind
            or provenance.get("attempt_kind") != derived_kind
        ):
            raise ValueError("durable cost attempt kind is inconsistent")
        if row["ordinal"] > 0:
            if derived_kind == "retry" and provenance.get("provider") != prior["provider"]:
                raise ValueError("durable cost retry provider is inconsistent")
            if derived_kind == "failover":
                pool = json.loads(row["provider_pool_json"])
                expected_provider = pool[min(row["ordinal"], len(pool) - 1)]
                if provenance.get("provider") != expected_provider:
                    raise ValueError("durable cost failover provider is inconsistent")
        expected_usage_source = (
            "verified_actual" if row["usage_verified"] == 1 else "reservation"
        )
        if row["outcome"] is not None and row["usage_source"] != expected_usage_source:
            raise ValueError("durable cost usage-source parity is incomplete")
        expected_outcome = (
            "success" if row["completion_outcome"] == "valid" else "failed"
        ) if row["completion_id"] is not None else row["outcome"]
        if row["outcome"] is not None and row["outcome"] != expected_outcome:
            raise ValueError("durable cost outcome parity is incomplete")
        verified_authority = None
        if row["usage_verified"] == 1 and row["actual_json"] is not None:
            verified_authority = (
                history_audit_store._verified_usage_authority_for_settlement(
                    conn,
                    attempt_id=row["attempt_id"],
                    actual_json=row["actual_json"],
                    terminal_at=row["budget_created_at"],
                )
            )
            if (
                verified_authority is None
                or verified_authority["terminal_at"]
                    != row["terminal_completed_at"]
                or verified_authority["billing_state"]
                    != row["billing_state"]
                or verified_authority["price_source"]
                    != row["price_source"]
                or verified_authority["currency"] != row["currency"]
                or row["outcome"] != (
                    "success"
                    if verified_authority["terminal_outcome"] == "valid"
                    else "cancelled"
                    if verified_authority["terminal_outcome"] == "cancelled"
                    else "failed"
                )
                or (
                    row["completion_id"] is None
                    and (
                        verified_authority["terminal_outcome"] != "cancelled"
                        or verified_authority["output_cas_object_id"] is not None
                    )
                )
                or (
                    row["completion_id"] is not None
                    and (
                        verified_authority["terminal_outcome"]
                            != row["completion_outcome"]
                        or verified_authority["output_cas_object_id"]
                            != row["completion_output_id"]
                        or verified_authority["terminal_at"]
                            != row["completion_completed_at"]
                    )
                )
            ):
                raise ValueError(
                    "durable cost verified usage authority is incomplete"
                )
        elif not (
            row["usage_verified"] in (None, 0)
            and row["actual_json"] is None
        ):
            raise ValueError(
                "durable cost verified usage authority is incomplete"
            )
        try:
            reserved = json.loads(row["reserved_json"])
            actual = (
                json.loads(verified_authority["actual_json"])
                if verified_authority is not None else None
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("durable cost usage is invalid") from exc
        usage = actual if actual is not None else reserved
        allowed = {
            "input_tokens", "output_tokens", "cache_tokens",
            "provider_usage_units", "currency_micros",
        }
        if (
            not isinstance(usage, dict)
            or set(usage).difference(allowed)
            or any(type(value) is not int or value < 0 for value in usage.values())
        ):
            raise ValueError("durable cost usage is invalid")
        intent = row["intent"]
        realized = per_intent.setdefault(intent, _empty_cost())
        latency_complete.setdefault(intent, True)
        currency_complete.setdefault(intent, True)
        provider = provenance.get("provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError("durable cost provider is invalid")
        pool = json.loads(row["provider_pool_json"])
        if row["ordinal"] == 0 and provider != pool[0]:
            raise ValueError("durable cost initial provider is inconsistent")
        provider_target = provider_groups.setdefault((intent, provider), _empty_cost())
        route_fact = route_by_candidate.get((intent, row["candidate_id"]))
        slice_ids = json.loads(route_fact["risk_slices_json"]) if route_fact else []
        slice_targets = [
            slice_groups.setdefault((intent, slice_id), _empty_cost())
            for slice_id in slice_ids
        ]
        targets = [realized, provider_target] + slice_targets
        group_keys = [("intent", intent), ("provider", intent, provider)] + [
            ("slice", intent, slice_id) for slice_id in slice_ids
        ]
        for key in group_keys:
            group_completeness.setdefault(
                key, {"latency": True, "currency": True}
            )
        for target in targets:
            target["calls"] += 1
            if row["outcome"] == "failed":
                target["failed_calls"] += 1
            if row["outcome"] == "cancelled" and row["billing_state"] == "billable":
                target["billable_cancelled_calls"] += 1
        counter = row["attempt_kind"] + "_calls"
        if counter in realized:
            for target in targets:
                target[counter] += 1
        for field in (
            "input_tokens", "output_tokens", "cache_tokens",
            "provider_usage_units",
        ):
            for target in targets:
                target[field] += usage.get(field, 0)
        if row["queue_latency_ms"] is None or (
            row["outcome"] is not None and row["run_latency_ms"] is None
        ):
            latency_complete[intent] = False
            for key in group_keys:
                group_completeness[key]["latency"] = False
        else:
            for target in targets:
                target["queue_latency_ms"] += row["queue_latency_ms"]
                target["run_latency_ms"] += row["run_latency_ms"] or 0
        if row["outcome"] is None:
            currency_complete[intent] = False
            for key in group_keys:
                group_completeness[key]["currency"] = False
        elif row["billing_state"] != "nonbillable":
            if (
                row["billing_state"] != "billable"
                or row["price_source"] is None
                or row["currency"] is None
                or "currency_micros" not in usage
            ):
                currency_complete[intent] = False
                for key in group_keys:
                    group_completeness[key]["currency"] = False
            else:
                for target in targets:
                    target.setdefault("currency_micros", 0)
                    target["currency_micros"] += usage["currency_micros"]
        for target in targets:
            target["inflight_calls"] = target.get("inflight_calls", 0) + int(
                row["outcome"] is None
            )
            target["unverified_usage_calls"] = target.get(
                "unverified_usage_calls", 0
            ) + int(
                row["outcome"] is None or row["usage_source"] == "reservation"
            )
        prior_by_task[row["task_hash"]] = {
            "ordinal": row["ordinal"],
            "outcome": (
                row["completion_outcome"]
                if row["completion_outcome"] is not None else row["outcome"]
            ),
            "provider": provenance.get("provider"),
        }
    result = {"run_id": run_id, "intents": {}}
    for intent, realized in sorted(per_intent.items()):
        route_facts_complete = route_facts_complete_by_intent.get(
            intent, False
        )
        derived_availability = _derived_attempt_kind_availability(
            conn, run_id, intent
        )
        latency_complete.setdefault(intent, True)
        currency_complete.setdefault(intent, True)
        if not latency_complete[intent]:
            realized.pop("queue_latency_ms", None)
            realized.pop("run_latency_ms", None)
        if not currency_complete[intent]:
            realized.pop("currency_micros", None)
        candidate_count = len(candidates[intent])
        facts = [
            row for row in route_rows if row["intent"] == intent
        ]
        escalated = sum(row["actual_l2_dispatch"] for row in facts)
        escalation_rate = escalated / candidate_count if candidate_count else 0.0
        required_l1_candidates = {
            row["candidate_id"] for row in facts if row["call_l1_model"]
        }
        intent_l1_rows = [
            row for row in l1_fact_rows if row["intent"] == intent
        ]
        l1_chain_complete = True
        for candidate_id in sorted(required_l1_candidates):
            chain = [
                row for row in intent_l1_rows
                if row["candidate_id"] == candidate_id
            ]
            if (
                not chain
                or [row["ordinal"] for row in chain] != list(range(len(chain)))
                or chain[0]["previous_attempt_id"] is not None
                or any(
                    chain[index]["previous_attempt_id"]
                        != chain[index - 1]["attempt_id"]
                    for index in range(1, len(chain))
                )
                or chain[-1]["outcome"] != "success"
                or any(row["outcome"] == "success" for row in chain[:-1])
            ):
                l1_chain_complete = False
                break
        intent_l1_currency_complete = l1_currency_complete.get(intent, True)
        intent_l1_latency_complete = l1_latency_complete.get(intent, True)
        l1_authority_complete = (
            not required_l1_candidates
            or (bool(intent_l1_rows) and l1_chain_complete)
        )
        combined_currency_complete = (
            currency_complete[intent]
            and l1_authority_complete
            and intent_l1_currency_complete
        )
        combined_latency_complete = (
            latency_complete[intent]
            and l1_authority_complete
            and intent_l1_latency_complete
        )
        expected = None
        expected_reason = "candidate_route_facts_unavailable"
        if (
            route_facts_complete
            and required_l1_candidates
            and not intent_l1_rows
        ):
            expected_reason = "durable_l1_attempt_facts_unavailable"
        elif (
            route_facts_complete
            and required_l1_candidates
            and not l1_chain_complete
        ):
            expected_reason = "durable_l1_attempt_facts_incomplete"
        elif route_facts_complete and escalated and realized["calls"] == 0:
            expected_reason = "durable_l2_cost_sample_unavailable"
        elif route_facts_complete:
            l1_total = l1_costs.get(intent, _empty_cost())
            l1_per_candidate = _per_unit_cost(
                l1_total, candidate_count,
                latency_complete=intent_l1_latency_complete,
                currency_complete=intent_l1_currency_complete,
            )
            l2_per_escalation = _per_unit_cost(
                realized, escalated,
                latency_complete=latency_complete[intent],
                currency_complete=currency_complete[intent],
            )
            expected = {
                "formula": (
                    "L1_per_candidate + escalation_rate * "
                    "L2_per_escalation"
                ),
                "escalation_rate": escalation_rate,
                "L1_per_candidate": l1_per_candidate,
                "L2_per_escalation": l2_per_escalation,
            }
            for field in _empty_cost():
                if (
                    field in l1_per_candidate
                    and field in l2_per_escalation
                ):
                    expected[field] = (
                        l1_per_candidate[field]
                        + escalation_rate * l2_per_escalation[field]
                    )
            if (
                combined_currency_complete
                and (
                    "currency_micros" in l1_per_candidate
                    or "currency_micros" in l2_per_escalation
                )
            ):
                expected["currency_micros"] = (
                    l1_per_candidate.get("currency_micros", 0)
                    + escalation_rate
                    * l2_per_escalation.get("currency_micros", 0)
                )
            expected_reason = None
        providers = {}
        for (group_intent, provider), cost in sorted(provider_groups.items()):
            if group_intent != intent:
                continue
            completeness = group_completeness[("provider", intent, provider)]
            if not completeness["latency"]:
                cost.pop("queue_latency_ms", None)
                cost.pop("run_latency_ms", None)
            if not completeness["currency"]:
                cost.pop("currency_micros", None)
            providers[provider] = {"realized": cost}
        risk_slices = None
        if route_facts_complete:
            risk_slices = {}
            slice_ids = sorted({
                slice_id for fact in facts
                for slice_id in json.loads(fact["risk_slices_json"])
            })
            for slice_id in slice_ids:
                slice_facts = [
                    fact for fact in facts
                    if slice_id in json.loads(fact["risk_slices_json"])
                ]
                cost = slice_groups.get((intent, slice_id), _empty_cost())
                completeness = group_completeness.get(
                    ("slice", intent, slice_id),
                    {"latency": True, "currency": True},
                )
                if not completeness["latency"]:
                    cost.pop("queue_latency_ms", None)
                    cost.pop("run_latency_ms", None)
                if not completeness["currency"]:
                    cost.pop("currency_micros", None)
                risk_slices[slice_id] = {
                    "candidate_count": len(slice_facts),
                    "escalated_candidate_count": sum(
                        fact["actual_l2_dispatch"] for fact in slice_facts
                    ),
                    "realized": cost,
                }
        result["intents"][intent] = {
            "candidate_count": candidate_count,
            "escalated_candidate_count": escalated,
            "escalation_rate": escalation_rate,
            "realized": realized,
            "accounting_complete": (
                realized.get("inflight_calls", 0) == 0
                and realized.get("unverified_usage_calls", 0) == 0
                and l1_costs.get(intent, {}).get(
                    "unverified_usage_calls", 0
                ) == 0
                and l1_authority_complete
                and combined_currency_complete
            ),
            "latency_complete": combined_latency_complete,
            "currency_complete": combined_currency_complete,
            "expected_per_candidate": expected,
            "expected_unavailable_reason": expected_reason,
            "risk_slices": risk_slices,
            "risk_slices_unavailable_reason": (
                None if route_facts_complete
                else "candidate_route_facts_unavailable"
            ),
            "providers": providers,
            "route_facts_complete": route_facts_complete,
            "route_observation_scope": (
                "host_issued_shadow" if route_facts_complete else None
            ),
            "route_observations_authorize_production": (
                False if route_facts_complete else None
            ),
            "route_observation_unavailable_reason": (
                None if route_facts_complete
                else "candidate_route_observation_boundary_unavailable"
            ),
            "attempt_kind_availability": {
                "initial": "durable", "retry": "durable",
                "failover": "durable", "split": "durable",
                **derived_availability,
            },
        }
    return result
