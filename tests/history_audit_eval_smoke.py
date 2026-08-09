#!/usr/bin/env python3
"""Behavioral smoke tests for semantic release, routing, and cost accounting."""

import copy
import contextlib
import hashlib
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from lib import history_audit
from lib import history_audit_plan
from lib import history_audit_store
from lib import history_cas
from lib import history_contract_v2 as contract
from lib import history_execution
from lib import history_store
from tests import history_audit_runtime_smoke as runtime_fixture

try:
    from lib import history_audit_eval_v2 as subject
except ImportError:
    subject = None


SHA = "a" * 64
SLICES = ("low_overlap", "cross_language", "lineage_revision")


def qrel(index, *, positive=True, partition="test", slices=(), scope="real"):
    quote = "shared evidence" if positive else "distinct evidence"
    relation = "blocking_duplicate" if positive else "distinct"
    return {
        "schema_version": "history-audit-qrel-v2",
        "qrel_id": "qrel-%04d" % index,
        "query_id": "query-%04d" % index,
        "query_lineage_id": "query-lineage-%04d" % index,
        "historical_id": "history-%04d" % index,
        "historical_lineage_id": "history-lineage-%04d" % index,
        "temporal_group": "time-%04d" % index,
        "as_of_sequence": 1000 + index,
        "historical_sequence": index,
        "semantic_relation": relation,
        "lineage_relation": "same_revision" if positive else "none",
        "historical_text": "prefix %s suffix" % quote,
        "evidence_anchors": [quote],
        "adjudication_state": "adjudicated",
        "negative_kind": None if positive else "hard_negative",
        "risk_slices": list(slices),
        "partition": partition,
        "scope": scope,
    }


def qrels(positive_count, negative_count, *, scope="real", slice_count=5):
    rows = []
    for index in range(positive_count):
        slices = sorted(name for name in SLICES if index < slice_count)
        rows.append(qrel(index, slices=slices, scope=scope))
    for index in range(positive_count, positive_count + negative_count):
        rows.append(qrel(index, positive=False, scope=scope))
    return rows


def outputs(rows, *, misses=()):
    missed = set(misses)
    return [
        {
            "query_id": row["query_id"],
            "historical_id": row["historical_id"],
            "semantic_relation": (
                "distinct"
                if row["query_lineage_id"] in missed
                else row["semantic_relation"]
            ),
        }
        for row in rows
    ]


def partitions(rows):
    result = {"train": [], "development": [], "test": []}
    for row in rows:
        result[row["partition"]].append(row["query_lineage_id"])
    return result


def policy():
    return {
        "schema_version": "semantic-release-policy-v1",
        "semantic_policy_profile_id": "semantic-test-v1",
        "wilson_one_sided_z": 1.6448536269514722,
        "shadow": {
            "minimum_positive_lineages": 30,
            "minimum_negative_lineages": 20,
            "critical_slices": {name: 5 for name in SLICES},
        },
        "production": {
            "minimum_positive_lineages": 300,
            "aggregate": {
                "minimum_observations": 300,
                "minimum_recall_lower_bound": 0.95,
                "maximum_false_negative_upper_bound": 0.05,
            },
            "required_slices": {
                name: {
                    "minimum_observations": 30,
                    "minimum_recall_lower_bound": 0.90,
                    "maximum_false_negative_upper_bound": 0.10,
                }
                for name in SLICES
            },
        },
    }


def evidence(*, basis="l2_exhaustive", dependency_overrides=None):
    pools = {name: ["fake"] for name in ("comparator", "map", "detail", "reduce")}
    dependencies = {
        "semantic_policy": subject.semantic_policy_sha256(policy()) if subject else "1" * 64,
        "plan": "1" * 64,
        "prompt": "2" * 64,
        "schema": "3" * 64,
        "ordered_provider_pools": contract.framed_sha256(
            "history-provider-pools-v2", contract.canonical_bytes(pools)
        ),
        "capacity": "5" * 64,
        "provider": contract.framed_sha256(
            "history-provider-capabilities-v2", contract.canonical_bytes(["7" * 64])
        ),
        "fault": "7" * 64,
        "replay": "8" * 64,
    }
    dependencies.update(dependency_overrides or {})
    value = {
        "no_match_basis": basis,
        "corpus_snapshot_hash": "9" * 64,
        "evaluation_hash": "b" * 64,
        "metric_report_hash": "c" * 64,
        "dependency_hashes": dependencies,
        "provider_capacity_complete": True,
        "fault_evidence_passed": True,
        "replay_evidence_passed": True,
        "expires_at": "2030-01-01T00:00:00+00:00",
    }
    if subject is not None:
        identity_rows = qrels(300, 20, slice_count=30)
        identities = subject.semantic_evaluation_identities(
            subject.validate_qrels(
                identity_rows, partitions(identity_rows), scope="real"
            ),
            outputs(identity_rows), policy(),
        )
        value["evaluation_hash"] = identities["evaluation_hash"]
        value["metric_report_hash"] = identities["metric_report_hash"]
    return value


def evaluation_evidence(
    rows, result_rows=None, *, scope="real", basis="l2_exhaustive",
    dependency_overrides=None,
):
    result_rows = outputs(rows) if result_rows is None else result_rows
    value = evidence(
        basis=basis, dependency_overrides=dependency_overrides
    )
    if subject is not None:
        identities = subject.semantic_evaluation_identities(
            subject.validate_qrels(rows, partitions(rows), scope=scope),
            result_rows,
            policy(),
        )
        value["evaluation_hash"] = identities["evaluation_hash"]
        value["metric_report_hash"] = identities["metric_report_hash"]
    return value


def production_fault_cases(*, recovered=True):
    baseline = {
        "state_sha256": "d" * 64,
        "completion_count": 1,
        "integrity_fault_count": 0,
    }
    recovery = copy.deepcopy(baseline)
    if not recovered:
        recovery["state_sha256"] = "c" * 64
    return [{
        "case_id": "fault-recovery-001",
        "scenario": "crash_after_durable_output",
        "baseline_result": baseline,
        "faulted_checkpoint_sha256": "e" * 64,
        "recovered_result": recovery,
    }]


def production_replay_cases(*, stable=True):
    first = {
        "state_sha256": "f" * 64,
        "completion_count": 1,
        "integrity_fault_count": 0,
    }
    replayed = copy.deepcopy(first)
    if not stable:
        replayed["completion_count"] = 2
    return [{
        "case_id": "restart-replay-001",
        "scenario": "restart_replay",
        "first_result": first,
        "replayed_result": replayed,
    }]


def production_evidence_roots(
    plan, *, fault_cases=None, replay_cases=None,
    expires_at="2030-01-01T00:00:00+00:00",
    include_semantic=True,
):
    fault_cases = fault_cases or production_fault_cases()
    replay_cases = replay_cases or production_replay_cases()
    _, fault_sha = history_audit_store._semantic_production_report(
        "fault", fault_cases, plan["plan_sha"]
    )
    _, replay_sha = history_audit_store._semantic_production_report(
        "replay", replay_cases, plan["plan_sha"]
    )
    dependencies = history_audit_store._semantic_plan_dependencies(
        dict(plan, plan_sha=plan["plan_sha"])
    )

    def entry(kind, report_sha):
        return {
            "schema_version": "history-production-evidence-root-v1",
            "evidence_kind": kind,
            "report_sha256": report_sha,
            "plan_sha": plan["plan_sha"],
            "capacity_profile_id": plan["capacity_profile_id"],
            "capacity_sha256": dependencies["capacity"],
            "provider_profile_hashes": dependencies[
                "provider_profile_hashes"
            ],
            "provider_sha256": dependencies["provider"],
            "ordered_provider_pools_sha256": dependencies[
                "ordered_provider_pools"
            ],
            "prompt_sha256": dependencies["prompt"],
            "schema_sha256": dependencies["schema"],
            "issuer_id": "future-test-host-evidence-root",
            "expires_at": expires_at,
        }

    semantic_entries = []
    if include_semantic:
        identity_rows = qrels(300, 20, slice_count=30)
        validated = subject.validate_qrels(
            identity_rows, partitions(identity_rows), scope="real"
        )
        identities = subject.semantic_evaluation_identities(
            validated, outputs(identity_rows), policy()
        )
        semantic_expiries = {expires_at}
        if expires_at == "2030-01-01T00:00:00+00:00":
            semantic_expiries.add("2026-08-03T00:00:02+00:00")
        for corpus_snapshot_hash in {
            "9" * 64, plan["snapshot"]["snapshot_hash"]
        }:
            for semantic_expires_at in semantic_expiries:
                semantic_entries.append({
                    "schema_version":
                        "history-semantic-evaluation-root-v1",
                    "qrels_hash": identities["qrels_hash"],
                    "evaluation_hash": identities["evaluation_hash"],
                    "metric_report_hash": identities[
                        "metric_report_hash"
                    ],
                    "plan_sha": plan["plan_sha"],
                    "corpus_snapshot_hash": corpus_snapshot_hash,
                    "semantic_policy_profile_id": plan[
                        "semantic_policy_profile_id"
                    ],
                    "policy_sha256": identities["policy_sha256"],
                    "no_match_basis": "l2_exhaustive",
                    "scope": "real",
                    "issuer_id": "future-test-host-semantic-root",
                    "expires_at": semantic_expires_at,
                })
        semantic_entries.sort(key=contract.canonical_bytes)
    return {
        "schema_version": "history-production-evidence-roots-v1",
        "registry_revision": "future-test-only",
        "fault_reports": [entry("fault", fault_sha)],
        "replay_reports": [entry("replay", replay_sha)],
        "semantic_evaluation_reports": semantic_entries,
    }


def empty_production_evidence_roots():
    return {
        "schema_version": "history-production-evidence-roots-v1",
        "registry_revision": "tracked-empty-test",
        "fault_reports": [],
        "replay_reports": [],
        "semantic_evaluation_reports": [],
    }


@contextlib.contextmanager
def future_production_evidence_roots(
    plan, *, fault_cases=None, replay_cases=None,
    expires_at="2030-01-01T00:00:00+00:00",
    include_semantic=True,
):
    roots = production_evidence_roots(
        plan, fault_cases=fault_cases, replay_cases=replay_cases,
        expires_at=expires_at, include_semantic=include_semantic,
    )
    with mock.patch.object(
        history_audit_store, "_load_production_evidence_roots",
        return_value=copy.deepcopy(roots), create=True,
    ):
        yield roots


def install_true_host_router_plan(conn, plan, *, persist=True):
    """Build the production preplan/router chain without test authorities."""
    candidate = plan["candidate"]
    if plan["snapshot"]["current_batch_ids"] != [candidate["candidate_id"]]:
        raise AssertionError("future production fixture requires one candidate")
    history_audit_store.record_host_router_preplan(
        conn,
        run_id=plan["run_id"], batch_id=plan["batch_id"],
        intent=plan["intent"],
        history_as_of_watermark=plan["snapshot"]["history_as_of_watermark"],
        exclusion_policy_sha=plan["snapshot"]["exclusion_policy_sha"],
        records=plan["snapshot"]["records"],
        candidates=[{
            "candidate_id": candidate["candidate_id"],
            "raw_artifact_sha": candidate["raw_artifact_sha"],
            "source_order": candidate["source_order"],
        }],
        created_at="2026-08-02T23:59:41+00:00",
    )
    route_round = history_audit_store.prepare_host_router_round(
        conn,
        run_id=plan["run_id"], batch_id=plan["batch_id"],
        intent=plan["intent"],
        raw_observations={
            "schema_version": "history-router-host-observations-v1",
            "selected_candidate_id": candidate["candidate_id"],
            "members": [{
                "candidate_id": candidate["candidate_id"],
                "selection_class": "finalist",
                "channel_states": [
                    {"channel_id": "dense_core", "state": "complete"},
                    {"channel_id": "exact_lineage", "state": "complete"},
                    {"channel_id": "fts", "state": "complete"},
                ],
                "assigned_slice_ids": ["low_overlap"],
                "permanent_request_id": None,
            }],
        },
        created_at="2026-08-02T23:59:45+00:00",
    )
    plan_dependencies = history_audit_store._semantic_plan_dependencies(
        dict(plan, plan_sha=plan["plan_sha"])
    )
    _, fault_sha = history_audit_store._semantic_production_report(
        "fault", production_fault_cases(), plan["plan_sha"]
    )
    _, replay_sha = history_audit_store._semantic_production_report(
        "replay", production_replay_cases(), plan["plan_sha"]
    )
    dependency_heads = (
        history_audit_store._router_host_default_dependency_heads(
            route_round["route_round_sha256"],
            history_audit_plan._host_runtime_authority(),
        )
    )
    dependency_heads.update({
        name: plan_dependencies[name]
        for name in (
            "plan", "prompt", "schema", "ordered_provider_pools",
            "capacity", "provider",
        )
    })
    dependency_heads.update({
        "semantic_policy": subject.semantic_policy_sha256(policy()),
        "fault": fault_sha,
        "replay": replay_sha,
    })
    history_audit_store.publish_semantic_dependency_heads(
        conn, dependency_heads, now="2026-08-02T23:59:45.500000+00:00"
    )
    history_audit_store.issue_host_router_domain_sources(
        conn, route_round["route_round_sha256"], phase="pre_l1",
        created_at="2026-08-02T23:59:46+00:00",
    )
    pre = history_audit_store.derive_candidate_route_facts(
        conn, plan["run_id"], plan["batch_id"], plan["intent"],
        phase="pre_l1", created_at="2026-08-02T23:59:47+00:00",
    )
    history_audit_store.issue_host_router_domain_sources(
        conn, route_round["route_round_sha256"], phase="final",
        created_at="2026-08-02T23:59:48+00:00",
    )
    final = history_audit_store.derive_candidate_route_facts(
        conn, plan["run_id"], plan["batch_id"], plan["intent"],
        phase="final", created_at="2026-08-02T23:59:49+00:00",
    )
    selected = final["candidate_routes"][0]
    if (
        selected["candidate_id"] != candidate["candidate_id"]
        or selected["matched_rule_ids"] != plan["matched_router_rule_ids"]
        or selected["risk_policy_version"] != plan["risk_policy_version"]
        or not selected["dispatch_allowed"]
    ):
        raise AssertionError("future production router binding is not exact")
    if persist:
        history_execution.persist_plan(conn, plan)
    return {
        "route_round": route_round,
        "pre": pre,
        "final": final,
    }


@contextlib.contextmanager
def future_production_plan(
    conn, *, approve_production_evidence=True, install_host_router=True,
    approve_semantic_evaluation=True,
):
    helper = runtime_fixture.HistoryAuditRuntimeSmoke(methodName="runTest")
    helper.records = [
        runtime_fixture.record("asset-1", "alpha evidence", "lineage-a"),
        runtime_fixture.record("asset-2", "beta evidence", "lineage-b"),
    ]
    helper.capabilities = {
        provider: {
            "provider": provider,
            "capability_profile_hash": runtime_fixture.sha(
                "capability-" + provider
            ),
            "model_identity": "fake-model-" + provider,
            "reasoning_identity": "high",
            "model_default": False,
            "reasoning_default": False,
            "executable": provider,
            "cli_revision": "fake-cli-v1",
        }
        for provider in ("codex", "grok", "reviewer")
    }
    plan = helper._plan(helper.records)
    helper.plan = plan
    helper.conn = conn
    profile = copy.deepcopy(plan["capacity_profile"])
    profile["profile_id"] = "future-production-safe-24k-v1"
    profile["status"] = "hard-complete"
    host = copy.deepcopy(history_audit_plan._host_runtime_authority())
    host["capacity_profiles"][profile["profile_id"]] = profile
    host["semantic_policy_profile_id"] = "semantic-test-v1"
    host_patch = mock.patch.object(
        history_audit_plan, "_host_runtime_authority",
        side_effect=lambda: copy.deepcopy(host),
    )
    with host_patch:
        authority = history_audit_plan._host_authority_for_capacity(
            profile["profile_id"]
        )
        plan.update({
            "capacity_profile_id": profile["profile_id"],
            "base_capacity_profile_id": profile["base_profile_id"],
            "capacity_profile": profile,
            "budget_policy": copy.deepcopy(authority["budget_policy"]),
            "semantic_policy_profile_id": authority[
                "semantic_policy_profile_id"
            ],
            "risk_policy_version": authority[
                "receipt_risk_policy_version"
            ],
            "settlement_policy_sha": authority["settlement_policy_sha"],
            "risk_policy_sha": authority["risk_policy_sha"],
            "authority_id": authority["authority_id"],
            "authority_scope": "production",
        })
        record_items = {
            item["item_id"]: item
            for item in history_audit_plan._record_items(
                plan["snapshot"]["records"]
            )
        }
        for shard in plan["shards"]:
            raw, count = history_audit_plan._serialized_request(
                plan["snapshot"],
                plan["candidate"],
                profile,
                [record_items[item_id] for item_id in shard["item_ids"]],
            )
            shard["serialized_request"] = raw.decode("utf-8")
            shard["request_sha256"] = hashlib.sha256(raw).hexdigest()
            shard["final_request_tokens"] = count
        plan["shard_plan_sha"] = history_audit_plan.runtime_shard_plan_sha(
            plan["shards"]
        )
        plan["plan_sha"] = history_audit_plan.runtime_plan_sha(plan)
        plan["logical_task_keys"] = [
            contract.logical_task_key(
                plan["plan_sha"], "map", plan["candidate"]["candidate_id"],
                shard["request_sha256"],
            )
            for shard in plan["shards"]
        ]
        if install_host_router:
            install_true_host_router_plan(conn, plan)
        capacity_sha = history_audit_store._semantic_sha(
            "history-capacity-profile-v1", profile
        )
        conn.execute(
            "INSERT INTO audit_capacity_profiles VALUES(?,?,?,?)",
            (
                profile["profile_id"], capacity_sha,
                contract.canonical_bytes(profile).decode("utf-8"),
                "2026-08-02T23:59:50+00:00",
            ),
        )
        for provider_name, capability in sorted(
            plan["provider_capabilities"].items()
        ):
            conn.execute(
                "INSERT INTO audit_provider_profiles VALUES(?,?,?,?)",
                (
                    capability["capability_profile_hash"], provider_name,
                    contract.canonical_bytes(capability).decode("utf-8"),
                    "2026-08-02T23:59:50+00:00",
                ),
            )
        conn.commit()
        roots_context = (
            future_production_evidence_roots(
                plan, include_semantic=approve_semantic_evaluation
            )
            if approve_production_evidence
            else contextlib.nullcontext()
        )
        with roots_context:
            yield helper, plan


def install_future_production_evidence(conn, plan):
    capacity_sha = history_audit_store._semantic_sha(
        "history-capacity-profile-v1", plan["capacity_profile"]
    )
    result = history_audit_store.issue_semantic_production_evidence(
        conn,
        plan_sha=plan["plan_sha"],
        fault_cases=production_fault_cases(),
        replay_cases=production_replay_cases(),
        now="2026-08-02T23:59:55+00:00",
    )
    assert result["dependency_hashes"]["capacity"] == capacity_sha
    return result


class QrelsAndReleaseTests(unittest.TestCase):
    def api(self, name):
        if subject is None:
            return lambda *args, **kwargs: None
        return getattr(subject, name)

    def validated(self, rows, scope="real"):
        return self.api("validate_qrels")(rows, partitions(rows), scope=scope)

    def test_qrels_reject_lineage_and_temporal_partition_leakage(self):
        rows = [qrel(0, partition="train"), qrel(1, partition="test")]
        rows[1]["query_lineage_id"] = rows[0]["query_lineage_id"]
        with self.assertRaises(ValueError):
            self.api("validate_qrels")(rows, partitions(rows), scope="real")

    def test_qrels_reject_role_swapped_lineage_partition_leakage(self):
        rows = [qrel(0, partition="train"), qrel(1, partition="test")]
        rows[1]["historical_lineage_id"] = rows[0]["query_lineage_id"]
        with self.assertRaises(ValueError):
            self.api("validate_qrels")(rows, partitions(rows), scope="real")

    def test_qrels_require_closed_independent_lineage_relation(self):
        missing = qrel(0)
        missing.pop("lineage_relation")
        with self.assertRaises(ValueError):
            self.validated([missing])
        invalid = qrel(1)
        invalid["lineage_relation"] = "blocking_duplicate"
        with self.assertRaises(ValueError):
            self.validated([invalid])
        rows = [qrel(0, partition="train"), qrel(1, partition="test")]
        rows[1]["temporal_group"] = rows[0]["temporal_group"]
        with self.assertRaises(ValueError):
            self.api("validate_qrels")(rows, partitions(rows), scope="real")

    def test_qrels_reject_future_duplicate_and_missing_semantic_evidence(self):
        future = qrel(0)
        future["historical_sequence"] = future["as_of_sequence"] + 1
        with self.assertRaises(ValueError):
            self.validated([future])
        duplicate = qrel(1)
        with self.assertRaises(ValueError):
            self.validated([duplicate, copy.deepcopy(duplicate)])
        for field in ("semantic_relation", "evidence_anchors"):
            broken = qrel(2)
            broken.pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validated([broken])

    def test_validated_qrels_replay_lineage_and_temporal_invariants(self):
        dataset = self.validated([qrel(0)])
        dataset["rows"][0]["historical_sequence"] = (
            dataset["rows"][0]["as_of_sequence"] + 1
        )
        unsigned = copy.deepcopy(dataset)
        unsigned.pop("qrels_hash")
        dataset["qrels_hash"] = subject._hash(
            "history-audit-qrels-v2", unsigned
        )
        with self.assertRaises(ValueError):
            self.api("evaluate_shadow_readiness")(
                dataset, outputs(dataset["rows"]), policy()
            )

    def test_outputs_are_bound_to_query_history_pairs(self):
        first = qrel(0)
        second = qrel(1, positive=False)
        second["query_id"] = first["query_id"]
        second["query_lineage_id"] = first["query_lineage_id"]
        data = self.api("validate_qrels")(
            [first, second],
            {"train": [], "development": [], "test": [first["query_lineage_id"]]},
            scope="real",
        )
        result = self.api("evaluate_shadow_readiness")(
            data, outputs([first, second]), policy()
        )
        self.assertEqual(result["counts"]["positive_lineages"], 1)

    def test_shadow_ready_has_exact_lineage_and_slice_boundaries(self):
        rows = qrels(30, 20, slice_count=5)
        result = self.api("evaluate_shadow_readiness")(
            self.validated(rows), outputs(rows), policy()
        )
        self.assertEqual(result["readiness_state"], "shadow_ready")
        self.assertFalse(result["production_qualified"])
        self.assertEqual(result["counts"]["positive_lineages"], 30)
        for changed in (qrels(29, 20), qrels(30, 19), qrels(30, 20, slice_count=4)):
            self.assertEqual(
                self.api("evaluate_shadow_readiness")(
                    self.validated(changed), outputs(changed), policy()
                )["readiness_state"],
                "not_ready",
            )

    def test_policy_cannot_lower_contractual_minima_or_change_fixed_z(self):
        rows = qrels(30, 20)
        for mutate in (
            lambda value: value["shadow"].update(minimum_positive_lineages=29),
            lambda value: value["production"].update(minimum_positive_lineages=299),
            lambda value: value.update(wilson_one_sided_z=1.96),
        ):
            lowered = policy()
            mutate(lowered)
            with self.assertRaises(ValueError):
                self.api("evaluate_shadow_readiness")(
                    self.validated(rows), outputs(rows), lowered
                )

    def test_diagnostic_or_synthetic_scope_never_qualifies_production(self):
        rows = qrels(300, 20, scope="diagnostic_synthetic", slice_count=30)
        result = self.api("evaluate_production_qualification")(
            self.validated(rows, "diagnostic_synthetic"),
            outputs(rows), policy(),
            evaluation_evidence(rows, scope="diagnostic_synthetic"),
        )
        self.assertFalse(result["production_qualified"])
        self.assertIn("non_production_scope", result["vetoes"])

    def test_production_requires_300_positives_all_slices_and_external_evidence(self):
        rows = qrels(300, 20, slice_count=30)
        result = self.api("evaluate_production_qualification")(
            self.validated(rows), outputs(rows), policy(), evidence()
        )
        self.assertTrue(result["production_qualified"])
        self.assertEqual(result["no_match_basis"], "l2_exhaustive")
        self.assertEqual(result["metrics"]["aggregate_recall"]["denominator"], 300)
        self.assertEqual(
            result["dependency_hashes"]["semantic_policy"], result["policy_sha256"]
        )
        self.assertEqual(
            {key: value for key, value in result["dependency_hashes"].items()
             if key != "semantic_policy"},
            {key: value for key, value in evidence()["dependency_hashes"].items()
             if key != "semantic_policy"},
        )
        under = qrels(299, 20, slice_count=30)
        result = self.api("evaluate_production_qualification")(
            self.validated(under), outputs(under), policy(),
            evaluation_evidence(under),
        )
        self.assertFalse(result["production_qualified"])
        self.assertIn("insufficient_positive_lineages", result["vetoes"])
        broken_evidence = evidence()
        broken_evidence["replay_evidence_passed"] = False
        result = self.api("evaluate_production_qualification")(
            self.validated(rows), outputs(rows), policy(), broken_evidence
        )
        self.assertIn("replay_evidence_failed", result["vetoes"])
        mismatched = evidence()
        mismatched["dependency_hashes"]["semantic_policy"] = "0" * 64
        with self.assertRaises(ValueError):
            self.api("evaluate_production_qualification")(
                self.validated(rows), outputs(rows), policy(), mismatched
            )

    def test_production_ignores_train_and_development_when_test_is_empty(self):
        rows = [
            qrel(
                index, partition="train",
                slices=sorted(SLICES) if index < 30 else (),
            )
            for index in range(300)
        ]
        rows.extend(
            qrel(index, positive=False, partition="development")
            for index in range(300, 320)
        )
        result = self.api("evaluate_production_qualification")(
            self.validated(rows), outputs(rows), policy(),
            evaluation_evidence(rows),
        )
        self.assertFalse(result["production_qualified"])
        self.assertEqual(
            result["metrics"]["aggregate_recall"]["denominator"], 0
        )
        self.assertEqual(result["metrics"]["negative_lineages"], 0)
        self.assertIn("insufficient_positive_lineages", result["vetoes"])
        self.assertIn("aggregate_abstain", result["vetoes"])

    def test_production_uses_only_held_out_test_metrics(self):
        train_rows = [
            qrel(
                index, partition="train",
                slices=sorted(SLICES) if index < 30 else (),
            )
            for index in range(300)
        ]
        test_rows = [
            qrel(
                index, partition="test",
                slices=sorted(SLICES) if index < 330 else (),
            )
            for index in range(300, 600)
        ]
        development_negatives = [
            qrel(index, positive=False, partition="development")
            for index in range(600, 620)
        ]
        test_negatives = [
            qrel(index, positive=False, partition="test")
            for index in range(620, 627)
        ]
        rows = train_rows + test_rows + development_negatives + test_negatives
        train_lineages = {
            row["query_lineage_id"] for row in train_rows
        }
        held_out_outputs = outputs(rows, misses=train_lineages)
        result = self.api("evaluate_production_qualification")(
            self.validated(rows), held_out_outputs,
            policy(), evaluation_evidence(rows, held_out_outputs),
        )
        self.assertTrue(result["production_qualified"])
        self.assertEqual(
            result["metrics"]["aggregate_recall"]["denominator"], 300
        )
        self.assertEqual(
            result["metrics"]["aggregate_recall"]["numerator"], 300
        )
        self.assertEqual(result["metrics"]["negative_lineages"], 7)
        held_out_identities = self.api("semantic_evaluation_identities")(
            self.validated(rows), outputs(rows, misses=train_lineages), policy()
        )
        all_correct_identities = self.api("semantic_evaluation_identities")(
            self.validated(rows), outputs(rows), policy()
        )
        self.assertEqual(
            held_out_identities["metrics"], all_correct_identities["metrics"]
        )
        self.assertNotEqual(
            held_out_identities["evaluation_hash"],
            all_correct_identities["evaluation_hash"],
        )

    def test_missing_bad_slice_abstains_and_vetoes(self):
        rows = qrels(300, 20, slice_count=29)
        result = self.api("evaluate_production_qualification")(
            self.validated(rows), outputs(rows), policy(),
            evaluation_evidence(rows),
        )
        self.assertFalse(result["production_qualified"])
        self.assertEqual(result["metrics"]["slices"]["low_overlap"]["state"], "abstain")
        self.assertIn("slice_low_overlap_abstain", result["vetoes"])

    def test_host_evaluator_persists_future_current_production_qualification(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(conn) as (_, plan):
                authority = install_future_production_evidence(conn, plan)
                evidence_value = evidence(
                    dependency_overrides=authority["dependency_hashes"]
                )
                evidence_value["corpus_snapshot_hash"] = plan["snapshot"][
                    "snapshot_hash"
                ]
                rows = qrels(300, 20, slice_count=30)
                qualification = self.api("evaluate_production_qualification")(
                    self.validated(rows), outputs(rows), policy(), evidence_value
                )
                history_audit_store.publish_semantic_dependency_heads(
                    conn, qualification["dependency_hashes"],
                    now="2026-08-02T23:59:59+00:00",
                )
                stored = history_audit_store.persist_semantic_qualification(
                    conn, self.validated(rows), outputs(rows), policy(),
                    evidence_value,
                    now="2026-08-03T00:00:00+00:00",
                )
                self.assertTrue(stored["production_qualified"])
                current = history_audit_store.lookup_semantic_qualification(
                    conn,
                    semantic_policy_profile_id=qualification[
                        "semantic_policy_profile_id"
                    ],
                    no_match_basis=qualification["no_match_basis"],
                    policy_sha256=qualification["policy_sha256"],
                    corpus_snapshot_hash=qualification["corpus_snapshot_hash"],
                    evaluation_hash=qualification["evaluation_hash"],
                    dependency_hashes=qualification["dependency_hashes"],
                    now="2026-08-03T00:00:01+00:00",
                )
                self.assertEqual(
                    current["qualification_id"], stored["qualification_id"]
                )
            conn.close()

    def test_repository_synthetic_qrels_cannot_self_label_real_and_mint(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(
                conn, approve_semantic_evaluation=False
            ) as (_, plan):
                authority = install_future_production_evidence(conn, plan)
                evidence_value = evidence(
                    dependency_overrides=authority["dependency_hashes"]
                )
                evidence_value["corpus_snapshot_hash"] = plan["snapshot"][
                    "snapshot_hash"
                ]
                rows = qrels(300, 20, slice_count=30, scope="real")
                qualification = self.api(
                    "evaluate_production_qualification"
                )(
                    self.validated(rows), outputs(rows), policy(),
                    evidence_value,
                )
                self.assertTrue(qualification["production_qualified"])
                history_audit_store.publish_semantic_dependency_heads(
                    conn, qualification["dependency_hashes"],
                    now="2026-08-02T23:59:59+00:00",
                )
                with self.assertRaisesRegex(
                    ValueError, "semantic evaluation root"
                ):
                    history_audit_store.persist_semantic_qualification(
                        conn, self.validated(rows), outputs(rows), policy(),
                        evidence_value,
                        now="2026-08-03T00:00:00+00:00",
                    )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM audit_semantic_qualifications"
                    ).fetchone()[0],
                    0,
                )
            conn.close()

    def test_one_byte_output_change_rejects_reused_evaluation_identity(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(conn) as (_, plan):
                authority = install_future_production_evidence(conn, plan)
                evidence_value = evidence(
                    dependency_overrides=authority["dependency_hashes"]
                )
                evidence_value["corpus_snapshot_hash"] = plan["snapshot"][
                    "snapshot_hash"
                ]
                rows = qrels(300, 20, slice_count=30)
                changed_outputs = outputs(rows)
                changed_outputs[-1]["semantic_relation"] = "related_only"
                with self.assertRaisesRegex(
                    ValueError, "production evaluation identity is invalid"
                ):
                    self.api("evaluate_production_qualification")(
                        self.validated(rows), changed_outputs, policy(),
                        evidence_value,
                    )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM audit_semantic_qualifications"
                    ).fetchone()[0],
                    0,
                )
            conn.close()

    def test_caller_self_rehashed_evaluation_and_metrics_cannot_mint(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(conn) as (_, plan):
                authority = install_future_production_evidence(conn, plan)
                evidence_value = evidence(
                    dependency_overrides=authority["dependency_hashes"]
                )
                evidence_value["corpus_snapshot_hash"] = plan["snapshot"][
                    "snapshot_hash"
                ]
                rows = qrels(300, 20, slice_count=30)
                result_rows = outputs(rows)
                evidence_value["evaluation_hash"] = contract.framed_sha256(
                    "caller-self-rehashed-evaluation-v1",
                    contract.canonical_bytes(result_rows),
                )
                evidence_value["metric_report_hash"] = contract.framed_sha256(
                    "caller-self-rehashed-metrics-v1",
                    contract.canonical_bytes({"passed": True}),
                )
                with self.assertRaisesRegex(
                    ValueError, "production evaluation identity is invalid"
                ):
                    self.api("evaluate_production_qualification")(
                        self.validated(rows), result_rows, policy(),
                        evidence_value,
                    )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM audit_semantic_qualifications"
                    ).fetchone()[0],
                    0,
                )
            conn.close()

    def test_caller_boolean_gates_and_heads_cannot_mint_production(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            rows = qrels(300, 20, slice_count=30)
            qualification = self.api("evaluate_production_qualification")(
                self.validated(rows), outputs(rows), policy(), evidence()
            )
            self.assertTrue(qualification["production_qualified"])
            history_audit_store.publish_semantic_dependency_heads(
                conn, qualification["dependency_hashes"],
                now="2026-08-02T23:59:59+00:00",
            )
            with self.assertRaisesRegex(
                ValueError, "durable production evidence is unavailable"
            ):
                history_audit_store.persist_semantic_qualification(
                    conn, self.validated(rows), outputs(rows), policy(),
                    evidence(), now="2026-08-03T00:00:00+00:00",
                )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM audit_semantic_qualifications"
                ).fetchone()[0],
                0,
            )
            conn.close()

    def test_profiles_and_caller_cases_without_durable_plan_cannot_mint(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            profile = copy.deepcopy(
                json.loads(
                    (ROOT / "history/capacity-profiles-v1.json").read_text(
                        encoding="utf-8"
                    )
                )["profiles"]["fake-safe-24k-v1"]
            )
            profile["profile_id"] = "caller-production-profile-v1"
            profile["status"] = "hard-complete"
            conn.execute(
                "INSERT INTO audit_capacity_profiles VALUES(?,?,?,?)",
                (
                    profile["profile_id"],
                    history_audit_store._semantic_sha(
                        "history-capacity-profile-v1", profile
                    ),
                    contract.canonical_bytes(profile).decode("utf-8"),
                    "2026-08-02T23:59:50+00:00",
                ),
            )
            conn.commit()
            with self.assertRaisesRegex(
                ValueError, "production plan evidence is unavailable"
            ):
                history_audit_store.issue_semantic_production_evidence(
                    conn, plan_sha="1" * 64,
                    fault_cases=production_fault_cases(),
                    replay_cases=production_replay_cases(),
                    now="2026-08-02T23:59:55+00:00",
                )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM audit_semantic_production_evidence_v2"
                ).fetchone()[0],
                0,
            )
            conn.close()

    def test_equal_self_reports_without_host_roots_cannot_mint(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(
                conn, approve_production_evidence=False
            ) as (_, plan):
                with self.assertRaisesRegex(
                    ValueError, "production evidence root"
                ):
                    history_audit_store.issue_semantic_production_evidence(
                        conn, plan_sha=plan["plan_sha"],
                        fault_cases=production_fault_cases(),
                        replay_cases=production_replay_cases(),
                        now="2026-08-02T23:59:55+00:00",
                    )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) "
                        "FROM audit_semantic_production_evidence_v2"
                    ).fetchone()[0],
                    0,
                )
            conn.close()

    def test_production_evidence_requires_exact_report_and_plan_profile_roots(self):
        def mutate_fault_report_byte(roots):
            original = roots["fault_reports"][0]["report_sha256"]
            replacement = "0" if original[0] != "0" else "1"
            roots["fault_reports"][0]["report_sha256"] = (
                replacement + original[1:]
            )

        mutations = {
            "fault_report_byte": mutate_fault_report_byte,
            "plan": lambda roots: roots["replay_reports"][0].update(
                plan_sha="0" * 64
            ),
            "provider_profile": lambda roots: roots["fault_reports"][0].update(
                provider_sha256="0" * 64
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(root_binding=name), tempfile.TemporaryDirectory() as root:
                conn = history_store.connect(
                    pathlib.Path(root) / "history.sqlite3"
                )
                history_store.init_schema(conn)
                history_audit_store.init_schema(conn)
                with future_production_plan(
                    conn, approve_production_evidence=False
                ) as (_, plan):
                    roots = production_evidence_roots(plan)
                    mutate(roots)
                    with mock.patch.object(
                        history_audit_store,
                        "_load_production_evidence_roots",
                        return_value=copy.deepcopy(roots), create=True,
                    ):
                        with self.assertRaisesRegex(
                            ValueError, "production evidence root"
                        ):
                            history_audit_store.issue_semantic_production_evidence(
                                conn, plan_sha=plan["plan_sha"],
                                fault_cases=production_fault_cases(),
                                replay_cases=production_replay_cases(),
                                now="2026-08-02T23:59:55+00:00",
                            )
                    self.assertEqual(
                        conn.execute(
                            "SELECT count(*) "
                            "FROM audit_semantic_production_evidence_v2"
                        ).fetchone()[0],
                        0,
                    )
                conn.close()

    def test_same_plan_accepts_new_current_fault_replay_generation(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(
                conn, approve_production_evidence=False
            ) as (_, plan):
                first_fault = production_fault_cases()
                first_replay = production_replay_cases()
                with future_production_evidence_roots(
                    plan,
                    fault_cases=first_fault,
                    replay_cases=first_replay,
                ):
                    first = history_audit_store.issue_semantic_production_evidence(
                        conn,
                        plan_sha=plan["plan_sha"],
                        fault_cases=first_fault,
                        replay_cases=first_replay,
                        now="2026-08-02T23:59:55+00:00",
                    )

                second_fault = production_fault_cases()
                second_fault[0]["case_id"] = "fault-recovery-002"
                second_replay = production_replay_cases()
                second_replay[0]["case_id"] = "restart-replay-002"
                with future_production_evidence_roots(
                    plan,
                    fault_cases=second_fault,
                    replay_cases=second_replay,
                ):
                    second = history_audit_store.issue_semantic_production_evidence(
                        conn,
                        plan_sha=plan["plan_sha"],
                        fault_cases=second_fault,
                        replay_cases=second_replay,
                        now="2026-08-02T23:59:56+00:00",
                    )
                    self.assertEqual(
                        history_audit_store._require_durable_semantic_production_evidence(
                            conn, second["dependency_hashes"]
                        ),
                        second["evidence_id"],
                    )

                self.assertNotEqual(first["evidence_id"], second["evidence_id"])
                rows = conn.execute(
                    "SELECT fault_sha256,replay_sha256 "
                    "FROM audit_semantic_production_evidence_v2 "
                    "WHERE plan_sha=? ORDER BY created_at",
                    (plan["plan_sha"],),
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertNotEqual(tuple(rows[0]), tuple(rows[1]))
            conn.close()

    def test_same_plan_same_report_pair_reissue_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(conn) as (_, plan):
                first = history_audit_store.issue_semantic_production_evidence(
                    conn,
                    plan_sha=plan["plan_sha"],
                    fault_cases=production_fault_cases(),
                    replay_cases=production_replay_cases(),
                    now="2026-08-02T23:59:55+00:00",
                )
                replay = history_audit_store.issue_semantic_production_evidence(
                    conn,
                    plan_sha=plan["plan_sha"],
                    fault_cases=production_fault_cases(),
                    replay_cases=production_replay_cases(),
                    now="2026-08-02T23:59:56+00:00",
                )
                self.assertEqual(replay["evidence_id"], first["evidence_id"])
                rows = conn.execute(
                    "SELECT evidence_id,created_at "
                    "FROM audit_semantic_production_evidence_v2 "
                    "WHERE plan_sha=?",
                    (plan["plan_sha"],),
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in rows],
                    [(first["evidence_id"], "2026-08-02T23:59:55+00:00")],
                )
            conn.close()

    def test_expired_production_evidence_root_cannot_mint(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(
                conn, approve_production_evidence=False
            ) as (_, plan), future_production_evidence_roots(
                plan, expires_at="2026-08-02T23:59:59+00:00"
            ), mock.patch.object(
                history_audit_store, "_utc_now",
                return_value="2026-08-03T00:00:00+00:00",
            ):
                with self.assertRaisesRegex(
                    ValueError, "production evidence root"
                ):
                    history_audit_store.issue_semantic_production_evidence(
                        conn, plan_sha=plan["plan_sha"],
                        fault_cases=production_fault_cases(),
                        replay_cases=production_replay_cases(),
                        now="2026-08-02T23:59:55+00:00",
                    )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) "
                        "FROM audit_semantic_production_evidence_v2"
                    ).fetchone()[0],
                    0,
                )
            conn.close()

    def test_direct_sql_cannot_insert_semantic_production_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO audit_semantic_production_evidence_v2 "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "0" * 64, "1" * 64, "capacity", "2" * 64,
                        "[]", "3" * 64, "4" * 64, "5" * 64,
                        "6" * 64, "{}", "7" * 64, "{}", "8" * 64,
                        "2026-08-03T00:00:00+00:00",
                    ),
                )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) "
                    "FROM audit_semantic_production_evidence_v2"
                ).fetchone()[0],
                0,
            )
            conn.close()

    def test_caller_outcome_field_is_not_raw_fault_authority(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(conn) as (_, plan):
                forged = production_fault_cases()
                forged[0]["outcome"] = "passed"
                with self.assertRaisesRegex(
                    ValueError, "semantic production evidence case is invalid"
                ):
                    history_audit_store.issue_semantic_production_evidence(
                        conn, plan_sha=plan["plan_sha"], fault_cases=forged,
                        replay_cases=production_replay_cases(),
                        now="2026-08-02T23:59:55+00:00",
                    )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) "
                        "FROM audit_semantic_production_evidence_v2"
                    ).fetchone()[0],
                    0,
                )
            conn.close()

    def test_failed_replay_cannot_mint_even_with_exact_host_root(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            failed_replay = production_replay_cases(stable=False)
            with future_production_plan(
                conn, approve_production_evidence=False
            ) as (_, plan), future_production_evidence_roots(
                plan, replay_cases=failed_replay
            ):
                with self.assertRaisesRegex(
                    ValueError, "production evidence did not pass"
                ):
                    history_audit_store.issue_semantic_production_evidence(
                        conn, plan_sha=plan["plan_sha"],
                        fault_cases=production_fault_cases(),
                        replay_cases=failed_replay,
                        now="2026-08-02T23:59:55+00:00",
                    )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) "
                        "FROM audit_semantic_production_evidence_v2"
                    ).fetchone()[0],
                    0,
                )
            conn.close()

    def test_host_evaluator_rejects_expired_production_qualification(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(conn) as (_, plan):
                authority = install_future_production_evidence(conn, plan)
                rows = qrels(300, 20, slice_count=30)
                expired_evidence = evidence(
                    dependency_overrides=authority["dependency_hashes"]
                )
                expired_evidence["corpus_snapshot_hash"] = plan["snapshot"][
                    "snapshot_hash"
                ]
                expired_evidence["expires_at"] = "2026-08-02T23:59:59+00:00"
                with self.assertRaisesRegex(
                    ValueError, "production evidence is expired"
                ):
                    self.api("evaluate_production_qualification")(
                        self.validated(rows), outputs(rows), policy(),
                        expired_evidence,
                    )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM audit_semantic_qualifications"
                ).fetchone()[0],
                0,
            )
            conn.close()

    def test_caller_time_cannot_backdate_production_qualification(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with future_production_plan(conn) as (_, plan):
                authority = install_future_production_evidence(conn, plan)
                evidence_value = evidence(
                    dependency_overrides=authority["dependency_hashes"]
                )
                evidence_value["corpus_snapshot_hash"] = plan["snapshot"][
                    "snapshot_hash"
                ]
                evidence_value["expires_at"] = (
                    "2026-08-03T00:00:02+00:00"
                )
                rows = qrels(300, 20, slice_count=30)
                with self.assertRaisesRegex(
                    ValueError, "production evidence is expired"
                ):
                    self.api("evaluate_production_qualification")(
                        self.validated(rows), outputs(rows), policy(),
                        evidence_value,
                    )
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM audit_semantic_qualifications"
                ).fetchone()[0],
                0,
            )
            conn.close()

    def test_host_evaluator_rejects_stale_plan_dependency_heads_locally(self):
        for dependency in (
            "provider", "capacity", "prompt", "schema",
            "ordered_provider_pools",
        ):
            with self.subTest(dependency=dependency), tempfile.TemporaryDirectory() as root:
                conn = history_store.connect(
                    pathlib.Path(root) / "history.sqlite3"
                )
                history_store.init_schema(conn)
                history_audit_store.init_schema(conn)
                with future_production_plan(conn) as (_, plan):
                    authority = install_future_production_evidence(conn, plan)
                    evidence_value = evidence(
                        dependency_overrides=authority["dependency_hashes"]
                    )
                    evidence_value["corpus_snapshot_hash"] = plan["snapshot"][
                        "snapshot_hash"
                    ]
                    rows = qrels(300, 20, slice_count=30)
                    qualification = self.api(
                        "evaluate_production_qualification"
                    )(
                        self.validated(rows), outputs(rows), policy(),
                        evidence_value,
                    )
                    history_audit_store.publish_semantic_dependency_heads(
                        conn, qualification["dependency_hashes"],
                        now="2026-08-02T23:59:58+00:00",
                    )
                    heads_before = (
                        history_audit_store.current_semantic_dependency_heads(
                            conn
                        )
                    )
                    fts_before = heads_before["fts"]
                    history_audit_store.publish_semantic_dependency_heads(
                        conn, {dependency: "0" * 64},
                        now="2026-08-02T23:59:59+00:00",
                    )
                    heads_after = (
                        history_audit_store.current_semantic_dependency_heads(
                            conn
                        )
                    )
                    self.assertEqual(heads_after["fts"], fts_before)
                    self.assertEqual(
                        {
                            name for name in heads_before
                            if heads_before[name] != heads_after[name]
                        },
                        {dependency},
                    )
                    with self.assertRaisesRegex(
                        ValueError, "qualification dependencies are not current"
                    ):
                        history_audit_store.persist_semantic_qualification(
                            conn, self.validated(rows), outputs(rows), policy(),
                            evidence_value, now="2026-08-03T00:00:00+00:00",
                        )
                self.assertEqual(
                    conn.execute(
                        "SELECT count(*) FROM audit_semantic_qualifications"
                    ).fetchone()[0],
                    0,
                )
                conn.close()

    def test_diagnostic_qualification_persists_and_stays_invalidated(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            rows = qrels(
                300, 20, scope="diagnostic_synthetic", slice_count=30
            )
            validated = self.validated(rows, "diagnostic_synthetic")
            evidence_value = evaluation_evidence(
                rows, scope="diagnostic_synthetic"
            )
            qualification = self.api("evaluate_production_qualification")(
                validated, outputs(rows), policy(), evidence_value
            )
            self.assertFalse(qualification["production_qualified"])
            history_audit_store.publish_semantic_dependency_heads(
                conn, qualification["dependency_hashes"],
                now="2026-08-02T23:59:59+00:00",
            )
            stored = history_audit_store.persist_semantic_qualification(
                conn, validated, outputs(rows), policy(), evidence_value,
                now="2026-08-03T00:00:00+00:00",
            )
            self.assertFalse(stored["production_qualified"])
            fact = conn.execute(
                "SELECT production_qualified,vetoes_json "
                "FROM audit_semantic_qualification_facts_v2 "
                "WHERE qualification_id=?",
                (stored["qualification_id"],),
            ).fetchone()
            self.assertEqual(fact["production_qualified"], 0)
            self.assertIn("non_production_scope", json.loads(fact["vetoes_json"]))
            changed = dict(qualification["dependency_hashes"], provider="d" * 64)
            self.assertIsNone(history_audit_store.lookup_semantic_qualification(
                conn,
                semantic_policy_profile_id=qualification["semantic_policy_profile_id"],
                no_match_basis="l2_exhaustive",
                policy_sha256=qualification["policy_sha256"],
                corpus_snapshot_hash=qualification["corpus_snapshot_hash"],
                evaluation_hash=qualification["evaluation_hash"],
                dependency_hashes=changed,
                now="2026-08-03T00:00:01+00:00",
            ))
            invalidation = history_audit_store.record_qualification_invalidation(
                conn, stored["qualification_id"], {"provider": "d" * 64},
                now="2026-08-03T00:00:02+00:00"
            )
            self.assertTrue(invalidation["impacts"]["adjudication_stale"])
            self.assertEqual(invalidation["impacts"]["search_generations_stale"], [])
            with self.assertRaises(ValueError):
                history_audit_store.record_qualification_invalidation(
                    conn, stored["qualification_id"], {"unbound": "e" * 64},
                    now="2026-08-03T00:00:02+00:00"
                )
            self.assertIsNone(history_audit_store.lookup_semantic_qualification(
                conn,
                semantic_policy_profile_id=qualification["semantic_policy_profile_id"],
                no_match_basis="l2_exhaustive",
                policy_sha256=qualification["policy_sha256"],
                corpus_snapshot_hash=qualification["corpus_snapshot_hash"],
                evaluation_hash=qualification["evaluation_hash"],
                dependency_hashes=qualification["dependency_hashes"],
                now="2026-08-03T00:00:03+00:00",
            ))
            conn.close()

    def test_forged_qualified_boolean_cannot_mint_complete_no_match(self):
        plan = {
            "snapshot": {
                "records": [{"item_id": "asset-1", "lineage_id": "lineage-1"}]
            }
        }
        settlements = [{
            "state": "settled",
            "settlement_kind": "equal",
            "normalized_result": {"items": [{
                "item_id": "asset-1", "lineage_id": "lineage-1",
                "semantic_relation": "distinct", "lineage_relation": "none",
                "anchor": "distinct evidence",
            }]},
        }]
        receipt = history_audit.summarize_l2_coverage(
            plan, settlements, {"qualified": True, "profile_id": "forged"}
        )
        self.assertEqual(receipt["final_status"], "uncertain")
        self.assertEqual(receipt["stage_reason_code"], "semantic_policy_unqualified")

    def test_l1_receipt_also_rejects_forged_qualified_boolean(self):
        snapshot = {
            "snapshot_id": "1" * 64, "snapshot_hash": "2" * 64,
            "history_as_of_watermark": 1,
            "current_batch_id_namespace": "history-v2-staging-v1",
            "current_batch_ids_hash": "3" * 64,
            "exclusion_policy_sha": "4" * 64,
            "expected_asset_ids_hash": "5" * 64,
        }
        retrieval = {
            "run_id": "run", "plan_hash": "6" * 64,
            "candidate_hash": "7" * 64, "observed_asset_ids_hash": "5" * 64,
            "missing_ids": [], "duplicate_ids": [], "extra_ids": [],
            "invalid_schema": False, "invalid_anchor": False, "truncated": False,
            "provider_pools_ordered": {name: ["fake"] for name in ("comparator", "map", "detail", "reduce")},
            "provider_capability_profile_hashes": ["8" * 64],
            "capacity_profile_id": "capacity", "risk_policy_version": "risk",
            "matched_router_rule_ids": [], "settlement_policy_sha": "9" * 64,
            "shard_plan_sha": "a" * 64, "logical_task_hashes": [],
            "attempt_manifest_hashes": [], "raw_request_output_cas_hashes": [],
            "minimum_receipt_sha": "b" * 64, "coverage_complete": True,
        }
        adjudication = {
            "adjudication_complete": True, "verified_hits": [],
            "unresolved_conflict": False, "exhausted_reason": None,
            "evidence_anchors": [],
        }
        forged = {
            "semantic_policy_profile_id": "forged",
            "semantic_policy_qualified": True,
            "no_match_basis": "l1_calibrated",
        }
        receipt = history_audit.build_l1_receipt(
            snapshot, retrieval, adjudication, forged
        )
        self.assertEqual(receipt["final_status"], "uncertain")
        self.assertFalse(receipt["semantic_policy_qualified"])

    def test_targeted_invalidation_preserves_fts_and_flat_reachability(self):
        qualification = {
            "dependency_hashes": dict(evidence()["dependency_hashes"], metadata="e" * 64)
        }
        provider = self.api("invalidate_qualification")(qualification, {"provider": "d" * 64})
        self.assertTrue(provider["adjudication_stale"])
        self.assertEqual(provider["search_generations_stale"], [])
        metadata = self.api("invalidate_qualification")(qualification, {"metadata": "f" * 64})
        self.assertEqual(metadata["search_generations_stale"], ["metadata"])
        self.assertFalse(metadata["flat_generation_stale"])


class RouterAndCostTests(unittest.TestCase):
    def api(self, name):
        if subject is None:
            return lambda *args, **kwargs: None
        return getattr(subject, name)

    def risk_policy(self):
        return json.loads((ROOT / "history/risk-policy-v1.json").read_text(encoding="utf-8"))

    def test_router_is_ordered_model_free_and_records_every_rule(self):
        facts = {
            "retriever_calibrated": False,
            "finalist_or_sa": True,
            "mandatory_channel_failed": False,
            "comparator_uncertain": True,
            "bad_slice_membership": True,
            "index_profile_recently_changed": False,
            "permanent_no_match_requested": True,
            "release_qualified": False,
            "candidate_budget_available": True,
            "attempt_budget_available": True,
        }
        before = copy.deepcopy(facts)
        result = self.api("route_candidate")(facts, self.risk_policy())
        self.assertEqual(facts, before)
        self.assertEqual(result["route"], "exhaustive")
        self.assertFalse(result["call_l1_model"])
        self.assertEqual(result["matched_rule_ids"], [
            "retriever_uncalibrated", "finalist_or_sa", "comparator_uncertain",
            "bad_slice_membership", "permanent_no_match_without_release_gate",
        ])
        self.assertIn(result["rule_table_sha256"], result["receipt_risk_policy_version"])

    def test_router_does_not_override_budget_or_release(self):
        facts = {
            "retriever_calibrated": False, "finalist_or_sa": False,
            "mandatory_channel_failed": False, "comparator_uncertain": False,
            "bad_slice_membership": False, "index_profile_recently_changed": False,
            "permanent_no_match_requested": True, "release_qualified": False,
            "candidate_budget_available": False, "attempt_budget_available": False,
        }
        result = self.api("route_candidate")(facts, self.risk_policy())
        self.assertEqual(result["route"], "exhaustive")
        self.assertFalse(result["dispatch_allowed"])
        self.assertFalse(result["release_authorized"])

    def test_release_veto_rule_only_matches_a_permanent_no_match_request(self):
        facts = {
            "retriever_calibrated": True, "finalist_or_sa": False,
            "mandatory_channel_failed": False, "comparator_uncertain": False,
            "bad_slice_membership": False, "index_profile_recently_changed": False,
            "permanent_no_match_requested": False, "release_qualified": False,
            "candidate_budget_available": True, "attempt_budget_available": True,
        }
        result = self.api("route_candidate")(facts, self.risk_policy())
        self.assertEqual(result["route"], "routine")
        self.assertNotIn(
            "permanent_no_match_without_release_gate", result["matched_rule_ids"]
        )

    def test_current_risk_policy_version_rejects_rewritten_rules(self):
        facts = {
            "retriever_calibrated": False, "finalist_or_sa": False,
            "mandatory_channel_failed": False, "comparator_uncertain": False,
            "bad_slice_membership": False, "index_profile_recently_changed": False,
            "permanent_no_match_requested": False, "release_qualified": False,
            "candidate_budget_available": True, "attempt_budget_available": True,
        }
        rewritten = self.risk_policy()
        rewritten["rules"] = [{
            "rule_id": "benign_rewrite", "fact": "finalist_or_sa",
            "equals": True, "required_route": "routine", "pre_l1": False,
        }]
        with self.assertRaises(ValueError):
            self.api("route_candidate")(facts, rewritten)

    def test_cost_summary_rejects_caller_constructed_event_lists(self):
        with self.assertRaises(TypeError):
            self.api("summarize_realized_cost")(
                {"attempt_events": [], "budget_events": [], "settlement_events": []},
                "run",
            )


class StorageReleaseAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name).resolve()
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        history_audit_store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def receipt(
        self, *, receipt_sha="d" * 64, snapshot_hash="9" * 64,
        basis="l2_exhaustive",
    ):
        receipt = {
            "manifest_schema_version": "history-audit-manifest-v2",
            "canonical_codec_version": "history-canonical-json-v2",
            "run_id": "release-run", "plan_hash": "1" * 64,
            "candidate_hash": "2" * 64, "snapshot_id": "3" * 64,
            "snapshot_hash": snapshot_hash, "history_as_of_watermark": 1,
            "current_batch_id_namespace": "history-v2-staging-v1",
            "current_batch_ids_hash": "4" * 64,
            "exclusion_policy_sha": "5" * 64,
            "expected_asset_ids_hash": "6" * 64,
            "observed_asset_ids_hash": "6" * 64,
            "missing_ids": [], "duplicate_ids": [], "extra_ids": [],
            "invalid_schema": False, "invalid_anchor": False,
            "truncated": False,
            "provider_pools_ordered": {
                name: ["fake"] for name in ("comparator", "map", "detail", "reduce")
            },
            "provider_capability_profile_hashes": ["7" * 64],
            "capacity_profile_id": "capacity", "semantic_policy_profile_id": "semantic-test-v1",
            "risk_policy_version": "risk", "matched_router_rule_ids": [],
            "settlement_policy_sha": "8" * 64, "shard_plan_sha": "a" * 64,
            "logical_task_hashes": [], "attempt_manifest_hashes": [],
            "raw_request_output_cas_hashes": [], "minimum_receipt_sha": receipt_sha,
            "coverage_complete": True, "adjudication_complete": True,
            "semantic_policy_qualified": True, "no_match_basis": basis,
            "final_status": "complete_no_match", "stage_reason_code": "complete_no_match",
            "evidence_anchors": [],
        }
        receipt["minimum_receipt_sha"] = contract.minimum_receipt_sha(receipt)
        return contract.validate_receipt(receipt)

    def install_owner(self, receipt):
        self.conn.execute(
            "INSERT OR IGNORE INTO audit_capacity_profiles(capacity_profile_id,profile_sha256,profile_json,created_at) VALUES(?,?,?,?)",
            (receipt["capacity_profile_id"], "5" * 64, "{}", "2026-08-03T00:00:00+00:00"),
        )
        for profile_hash in receipt["provider_capability_profile_hashes"]:
            self.conn.execute(
                "INSERT OR IGNORE INTO audit_provider_profiles(profile_hash,provider,profile_json,created_at) VALUES(?,?,?,?)",
                (profile_hash, "fake", "{}", "2026-08-03T00:00:00+00:00"),
            )
        self.conn.execute(
            "INSERT INTO audit_run_manifests(run_id,manifest_schema_version,plan_hash,manifest_json,created_at) "
            "VALUES(?, 'history-audit-manifest-v2', ?, '{}', '2026-08-03T00:00:00+00:00')",
            (receipt["run_id"], receipt["plan_hash"]),
        )
        self.conn.execute(
            """INSERT INTO audit_snapshots(
                 snapshot_id,snapshot_hash,history_as_of_watermark,
                 current_batch_id_namespace,current_batch_ids_hash,
                 exclusion_policy_sha,expected_asset_ids_hash,created_at,
                 run_id,batch_id
               ) VALUES(?,?,?,?,?,?,?,'2026-08-03T00:00:00+00:00',?,?)""",
            (
                receipt["snapshot_id"], receipt["snapshot_hash"],
                receipt["history_as_of_watermark"], receipt["current_batch_id_namespace"],
                receipt["current_batch_ids_hash"], receipt["exclusion_policy_sha"],
                receipt["expected_asset_ids_hash"], receipt["run_id"], "batch",
            ),
        )

    def direct_insert(self, receipt):
        fields = tuple(receipt)
        encoded = history_cas._receipt_row(receipt)
        self.conn.execute(
            "INSERT INTO audit_receipts(" + ",".join(fields) + ") VALUES(" +
            ",".join("?" for _ in fields) + ")",
            tuple(encoded[field] for field in fields),
        )

    def qualification(self, evidence_value=None):
        rows = qrels(300, 20, slice_count=30)
        return subject.evaluate_production_qualification(
            subject.validate_qrels(rows, partitions(rows), scope="real"),
            outputs(rows), policy(), evidence_value or evidence(),
        )

    def persist_qualification(self, *, now, evidence_value=None):
        rows = qrels(300, 20, slice_count=30)
        return history_audit_store.persist_semantic_qualification(
            self.conn,
            subject.validate_qrels(rows, partitions(rows), scope="real"),
            outputs(rows), policy(), evidence_value or evidence(), now=now,
        )

    def install_l2_execution(
        self, *, relations=None, qualification_expires_at=None,
    ):
        production_context = future_production_plan(self.conn)
        helper, plan = production_context.__enter__()
        self.addCleanup(
            lambda: production_context.__exit__(None, None, None)
        )
        output = helper._output(plan, relations=relations)

        def provider(*_):
            return {"kind": "success", "output": output}

        ready_at = self.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (plan["logical_task_keys"][0],),
        ).fetchone()[0]
        history_execution.run_map_task(
            self.conn, self.root / "cas", plan, plan["logical_task_keys"][0],
            provider, now=ready_at,
        )
        while True:
            pending = self.conn.execute(
                "SELECT task_hash,stage FROM audit_logical_tasks "
                "WHERE state='planned' AND stage IN ('detail','reduce') "
                "ORDER BY stage,task_hash LIMIT 1"
            ).fetchone()
            if pending is None:
                break
            task = history_execution.load_task(self.conn, pending["task_hash"])
            task_output = (
                helper._detail_output(task)
                if pending["stage"] == "detail"
                else helper._reduce_output(task)
            )
            history_execution.run_task(
                self.conn, self.root / "cas", plan, pending["task_hash"],
                lambda *_, task_output=task_output: {
                    "kind": "success", "output": task_output,
                },
                now=ready_at,
            )
        durable_summary = history_execution.build_coverage_receipt(
            plan,
            history_execution.load_terminal_states(self.conn, plan["plan_sha"]),
            {"qualified": False, "profile_id": plan["semantic_policy_profile_id"]},
            conn=self.conn,
        )
        production_evidence = install_future_production_evidence(
            self.conn, plan
        )
        profile_hashes = sorted(plan["provider_capability_profile_hashes"].values())
        dependencies = evidence()["dependency_hashes"]
        dependencies.update(production_evidence["dependency_hashes"])
        evidence_value = evidence()
        evidence_value["corpus_snapshot_hash"] = plan["snapshot"]["snapshot_hash"]
        evidence_value["dependency_hashes"] = dependencies
        if qualification_expires_at is not None:
            evidence_value["expires_at"] = qualification_expires_at
        receipt = self.receipt(
            snapshot_hash=plan["snapshot"]["snapshot_hash"]
        )
        route = self.conn.execute(
            "SELECT risk_policy_version,matched_rule_ids_json "
            "FROM audit_candidate_route_facts_v2 WHERE run_id=?",
            (plan["run_id"],),
        ).fetchone()
        receipt.update({
            "run_id": plan["run_id"], "plan_hash": plan["plan_sha"],
            "candidate_hash": plan["candidate"]["candidate_hash"],
            "snapshot_id": plan["snapshot"]["snapshot_id"],
            "history_as_of_watermark": plan["snapshot"]["history_as_of_watermark"],
            "current_batch_ids_hash": plan["snapshot"]["current_batch_ids_hash"],
            "exclusion_policy_sha": plan["snapshot"]["exclusion_policy_sha"],
            "expected_asset_ids_hash": plan["snapshot"]["expected_asset_ids_hash"],
            "observed_asset_ids_hash": contract.ordered_set_sha256(
                "history-observed-assets-v2",
                sorted(item["item_id"] for item in plan["snapshot"]["records"]),
            ),
            "provider_pools_ordered": plan["provider_pools_ordered"],
            "provider_capability_profile_hashes": profile_hashes,
            "capacity_profile_id": plan["capacity_profile_id"],
            "semantic_policy_profile_id": plan[
                "semantic_policy_profile_id"
            ],
            "risk_policy_version": route["risk_policy_version"],
            "matched_router_rule_ids": json.loads(
                route["matched_rule_ids_json"]
            ),
            "settlement_policy_sha": plan["settlement_policy_sha"],
            "shard_plan_sha": plan["shard_plan_sha"],
            "logical_task_hashes": [
                row[0] for row in self.conn.execute(
                    "SELECT task.task_hash FROM audit_logical_tasks task "
                    "JOIN audit_task_bindings_v2 binding "
                    "ON binding.task_hash=task.task_hash "
                    "WHERE binding.plan_sha=? ORDER BY task.task_hash",
                    (plan["plan_sha"],),
                )
            ],
            "evidence_anchors": durable_summary["evidence_anchors"],
        })
        attempts = self.conn.execute(
            "SELECT attempt_id,request_cas_object_id FROM audit_task_attempts "
            "ORDER BY attempt_id"
        ).fetchall()
        outputs_rows = self.conn.execute(
            "SELECT output_cas_object_id FROM audit_attempt_completions_v2"
        ).fetchall()
        receipt["attempt_manifest_hashes"] = [row[0] for row in attempts]
        receipt["raw_request_output_cas_hashes"] = sorted(
            {row[1] for row in attempts} | {row[0] for row in outputs_rows}
        )
        receipt["minimum_receipt_sha"] = contract.minimum_receipt_sha(receipt)
        return plan, evidence_value, contract.validate_receipt(receipt)

    def context(self, qualification):
        return {
            "scope": qualification["scope"],
            "policy_sha256": qualification["policy_sha256"],
            "corpus_snapshot_hash": qualification["corpus_snapshot_hash"],
            "evaluation_hash": qualification["evaluation_hash"],
            "dependency_hashes": qualification["dependency_hashes"],
        }

    def test_direct_complete_no_match_insert_requires_durable_authorization(self):
        receipt = self.receipt()
        self.install_owner(receipt)
        with self.assertRaises(Exception):
            self.direct_insert(receipt)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_receipts").fetchone()[0], 0
        )

    def test_all_current_no_match_bases_fail_closed_on_public_and_private_paths(self):
        for index, basis in enumerate(("l1_calibrated", "l2_exhaustive")):
            receipt = self.receipt(
                receipt_sha=hashlib.sha256(basis.encode()).hexdigest(), basis=basis
            )
            if index == 0:
                self.install_owner(receipt)
            with self.subTest(basis=basis):
                with self.assertRaises(history_cas.CASError):
                    history_cas.write_minimum_receipt(
                        self.conn, receipt, release_context={},
                        now="2026-08-03T00:00:00+00:00",
                    )
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    with self.assertRaisesRegex(
                        ValueError, "semantic release context is invalid"
                    ):
                        history_audit_store._authorize_complete_no_match_receipt(
                            self.conn, receipt, {},
                            now="2026-08-03T00:00:00+00:00",
                        )
                finally:
                    self.conn.execute("ROLLBACK")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_semantic_release_authorizations_v2"
            ).fetchone()[0], 0,
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_receipts").fetchone()[0], 0
        )

    def test_current_production_qualification_authorizes_and_replays_receipt(self):
        _, evidence_value, receipt = self.install_l2_execution()
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        stored = self.persist_qualification(
            now="2026-08-03T00:00:00+00:00",
            evidence_value=evidence_value,
        )
        self.assertTrue(stored["production_qualified"])
        receipt_id = history_cas.write_minimum_receipt(
            self.conn, receipt, release_context=self.context(qualification),
            now="2026-08-03T00:00:01+00:00",
        )
        self.assertEqual(
            receipt_id, receipt["minimum_receipt_sha"]
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_semantic_release_authorizations_v2"
            ).fetchone()[0], 1,
        )
        verified = history_cas.verify_minimum_receipt(
            self.conn, self.root / "cas", receipt_id,
            require_current_release=True,
            expected_context={
                field: receipt[field]
                for field in (
                    "run_id", "plan_hash", "candidate_hash",
                    "snapshot_id", "snapshot_hash",
                )
            },
            now="2026-08-03T00:00:02+00:00",
        )
        self.assertTrue(verified["historically_authorized"])
        self.assertTrue(verified["current_release_authority"])
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, {"provider": "0" * 64},
            now="2026-08-03T00:00:03+00:00",
        )
        historical = history_cas.verify_minimum_receipt(
            self.conn, self.root / "cas", receipt_id,
            require_current_release=False,
            now="2026-08-03T00:00:04+00:00",
        )
        self.assertTrue(historical["historically_authorized"])
        self.assertFalse(historical["current_release_authority"])
        with self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_minimum_receipt(
                self.conn, self.root / "cas", receipt_id,
                require_current_release=True,
                now="2026-08-03T00:00:04+00:00",
            )

    def test_durable_overlap_cannot_be_resigned_as_complete_no_match(self):
        _, evidence_value, forged = self.install_l2_execution(
            relations={
                "asset-1": "blocking_duplicate",
                "asset-2": "substantive_overlap",
            }
        )
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        stored = self.persist_qualification(
            now="2026-08-03T00:00:00+00:00",
            evidence_value=evidence_value,
        )
        self.assertTrue(stored["production_qualified"])

        with self.assertRaises(history_cas.CASError):
            history_cas.write_minimum_receipt(
                self.conn, forged,
                release_context=self.context(qualification),
                now="2026-08-03T00:00:01+00:00",
            )
        for table in (
            "audit_semantic_release_authorizations_v2",
            "audit_receipt_issuances_v2",
            "audit_receipts",
        ):
            self.assertEqual(
                self.conn.execute(
                    "SELECT count(*) FROM " + table
                ).fetchone()[0],
                0,
            )

    def test_root_removal_revokes_current_but_preserves_historical_receipt(self):
        plan, evidence_value, receipt = self.install_l2_execution()
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        self.persist_qualification(
            now="2026-08-03T00:00:00+00:00",
            evidence_value=evidence_value,
        )
        receipt_id = history_cas.write_minimum_receipt(
            self.conn, receipt,
            release_context=self.context(qualification),
            now="2026-08-03T00:00:01+00:00",
        )
        lookup_arguments = {
            "semantic_policy_profile_id": receipt[
                "semantic_policy_profile_id"
            ],
            "no_match_basis": qualification["no_match_basis"],
            "policy_sha256": qualification["policy_sha256"],
            "corpus_snapshot_hash": qualification["corpus_snapshot_hash"],
            "evaluation_hash": qualification["evaluation_hash"],
            "dependency_hashes": qualification["dependency_hashes"],
        }
        status_dependencies = {
            name: lookup_arguments[name]
            for name in (
                "no_match_basis", "policy_sha256", "corpus_snapshot_hash",
                "evaluation_hash", "dependency_hashes",
            )
        }

        def current_status():
            return history_audit.summarize_l2_coverage(
                plan,
                history_execution.load_terminal_states(
                    self.conn, plan["plan_sha"]
                ),
                {
                    "qualified": True,
                    "profile_id": receipt["semantic_policy_profile_id"],
                },
                qualification_conn=self.conn,
                qualification_dependencies=status_dependencies,
                now="2026-08-03T00:00:00+00:00",
                adjudication_state=history_execution.load_adjudication_state(
                    self.conn, plan["plan_sha"]
                ),
            )

        self.assertIsNotNone(
            history_audit_store.lookup_semantic_qualification(
                self.conn, **lookup_arguments,
                now="2026-08-03T00:00:00+00:00",
            )
        )
        self.assertEqual(current_status()["final_status"], "complete_no_match")

        removed_semantic_root = production_evidence_roots(plan)
        removed_semantic_root["semantic_evaluation_reports"] = []
        with mock.patch.object(
            history_audit_store, "_load_production_evidence_roots",
            return_value=removed_semantic_root, create=True,
        ):
            self.assertIsNone(
                history_audit_store.lookup_semantic_qualification(
                    self.conn, **lookup_arguments,
                    now="2026-08-03T00:00:00+00:00",
                )
            )
            downgraded = current_status()
            self.assertEqual(downgraded["final_status"], "uncertain")
            self.assertEqual(
                downgraded["stage_reason_code"],
                "semantic_policy_unqualified",
            )
            historical = history_cas.verify_minimum_receipt(
                self.conn, self.root / "cas", receipt_id,
                require_current_release=False,
                now="2026-08-03T00:00:02+00:00",
            )
            self.assertTrue(historical["historically_authorized"])
            self.assertFalse(historical["current_release_authority"])
            with self.assertRaises(history_cas.CASIntegrityError):
                history_cas.verify_minimum_receipt(
                    self.conn, self.root / "cas", receipt_id,
                    require_current_release=True,
                    now="2026-08-03T00:00:02+00:00",
                )

        swapped_roots = production_evidence_roots(plan)
        for root in swapped_roots["semantic_evaluation_reports"]:
            root["issuer_id"] = "replacement-host-semantic-root"
        with mock.patch.object(
            history_audit_store, "_load_production_evidence_roots",
            return_value=swapped_roots,
        ):
            self.assertIsNone(
                history_audit_store.lookup_semantic_qualification(
                    self.conn, **lookup_arguments,
                    now="2026-08-03T00:00:00+00:00",
                )
            )
            historical = history_cas.verify_minimum_receipt(
                self.conn, self.root / "cas", receipt_id,
                require_current_release=False,
                now="2026-08-03T00:00:02+00:00",
            )
            self.assertTrue(historical["historically_authorized"])
            with self.assertRaises(history_cas.CASIntegrityError):
                history_cas.verify_minimum_receipt(
                    self.conn, self.root / "cas", receipt_id,
                    require_current_release=True,
                    now="2026-08-03T00:00:02+00:00",
                )

        expired_roots = production_evidence_roots(plan)
        with mock.patch.object(
            history_audit_store, "_load_production_evidence_roots",
            return_value=expired_roots,
        ), mock.patch.object(
            history_audit_store, "_utc_now",
            return_value="2030-01-01T00:00:01+00:00",
        ):
            self.assertIsNone(
                history_audit_store.lookup_semantic_qualification(
                    self.conn, **lookup_arguments,
                    now="2026-08-03T00:00:00+00:00",
                )
            )
            self.assertEqual(current_status()["final_status"], "uncertain")
            historical = history_cas.verify_minimum_receipt(
                self.conn, self.root / "cas", receipt_id,
                require_current_release=False,
                now="2026-08-03T00:00:00+00:00",
            )
            self.assertTrue(historical["historically_authorized"])
            with self.assertRaises(history_cas.CASIntegrityError):
                history_cas.verify_minimum_receipt(
                    self.conn, self.root / "cas", receipt_id,
                    require_current_release=True,
                    now="2026-08-03T00:00:00+00:00",
                )

    def test_caller_time_cannot_backdate_release_authorization(self):
        _, evidence_value, receipt = self.install_l2_execution(
            qualification_expires_at="2030-01-01T00:00:00+00:00"
        )
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-03T00:00:00+00:00",
        )
        with mock.patch.object(
            history_audit_store, "_utc_now",
            return_value="2026-08-03T00:00:00+00:00",
        ):
            self.persist_qualification(
                now="2026-08-03T00:00:00+00:00",
                evidence_value=evidence_value,
            )
        with mock.patch.object(
            history_audit_store, "_utc_now",
            return_value="2030-01-01T00:00:01+00:00",
        ), self.assertRaises(history_cas.CASError):
            history_cas.write_minimum_receipt(
                self.conn, receipt,
                release_context=self.context(qualification),
                now="2026-08-03T00:00:01+00:00",
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) "
                "FROM audit_semantic_release_authorizations_v2"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_receipts"
            ).fetchone()[0],
            0,
        )

    def test_caller_time_cannot_backdate_current_release_verification(self):
        _, evidence_value, receipt = self.install_l2_execution(
            qualification_expires_at="2030-01-01T00:00:00+00:00"
        )
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-03T00:00:00+00:00",
        )
        with mock.patch.object(
            history_audit_store, "_utc_now",
            return_value="2026-08-03T00:00:00+00:00",
        ):
            self.persist_qualification(
                now="2026-08-03T00:00:00+00:00",
                evidence_value=evidence_value,
            )
        with mock.patch.object(
            history_audit_store, "_utc_now",
            return_value="2026-08-03T00:00:01+00:00",
        ):
            receipt_id = history_cas.write_minimum_receipt(
                self.conn, receipt,
                release_context=self.context(qualification),
                now="2026-08-03T00:00:01+00:00",
            )
        with mock.patch.object(
            history_audit_store, "_utc_now",
            return_value="2030-01-01T00:00:01+00:00",
        ), self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_minimum_receipt(
                self.conn, self.root / "cas", receipt_id,
                require_current_release=True,
                now="2026-08-03T00:00:01+00:00",
            )
        historical = history_cas.verify_minimum_receipt(
            self.conn, self.root / "cas", receipt_id,
            require_current_release=False,
            now="2026-08-03T00:00:01+00:00",
        )
        self.assertTrue(historical["historically_authorized"])

    def test_forged_qualification_material_cannot_create_release_authority(self):
        qualification = self.qualification()
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        forged = copy.deepcopy(qualification)
        forged["metrics"] = {}
        forged["evaluation_hash"] = "f" * 64
        with self.assertRaises(ValueError):
            history_audit_store.persist_semantic_qualification(
                self.conn, forged, now="2026-08-03T00:00:00+00:00"
            )
        with self.assertRaises(ValueError):
            history_audit_store._persist_semantic_qualification(
                self.conn, forged, now="2026-08-03T00:00:00+00:00"
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_semantic_qualifications"
            ).fetchone()[0],
            0,
        )

    def test_private_evaluator_guard_cannot_self_sign_production_evidence(self):
        qualification = self.qualification()
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        guard = history_audit_store._SEMANTIC_EVALUATION_GUARDS[id(self.conn)]
        guard["expected"] = history_audit_store._semantic_sha(
            "history-semantic-evaluator-issuance-v2", qualification
        )
        try:
            with self.assertRaisesRegex(
                ValueError, "qualification lacks evaluator issuance authority"
            ):
                history_audit_store._persist_semantic_qualification(
                    self.conn,
                    qualification,
                    now="2026-08-03T00:00:00+00:00",
                )
        finally:
            guard["expected"] = None
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_semantic_qualifications"
            ).fetchone()[0],
            0,
        )

    def test_dependency_head_change_revokes_issued_production_qualification(self):
        _, evidence_value, receipt = self.install_l2_execution()
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        stored = self.persist_qualification(
            now="2026-08-03T00:00:00+00:00",
            evidence_value=evidence_value,
        )
        self.assertTrue(stored["production_qualified"])
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, {"provider": "e" * 64}, now="2026-08-03T00:00:01+00:00"
        )
        with self.assertRaises(history_cas.CASError):
            history_cas.write_minimum_receipt(
                self.conn, receipt, release_context=self.context(qualification),
                now="2026-08-03T00:00:02+00:00",
            )

    def test_no_unavailable_runtime_receipt_can_enter_replay(self):
        _, evidence_value, receipt = self.install_l2_execution()
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        stored = self.persist_qualification(
            now="2026-08-03T00:00:00+00:00",
            evidence_value=evidence_value,
        )
        self.assertTrue(stored["production_qualified"])
        with self.assertRaises(history_cas.CASIntegrityError):
            history_cas.verify_minimum_receipt(
                self.conn, self.root / "cas", receipt["minimum_receipt_sha"],
                require_current_release=True,
                now="2026-08-03T00:00:03+00:00",
            )

    def test_empty_execution_sets_cannot_mint_complete_no_match(self):
        _, evidence_value, receipt = self.install_l2_execution()
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        stored = self.persist_qualification(
            now="2026-08-03T00:00:00+00:00",
            evidence_value=evidence_value,
        )
        self.assertTrue(stored["production_qualified"])
        forged = copy.deepcopy(receipt)
        forged["logical_task_hashes"] = []
        forged["attempt_manifest_hashes"] = []
        forged["raw_request_output_cas_hashes"] = []
        with self.assertRaises(history_cas.CASError):
            history_cas.write_minimum_receipt(
                self.conn, forged, release_context=self.context(qualification),
                now="2026-08-03T00:00:01+00:00",
            )

    def test_changed_heads_do_not_open_production_persistence(self):
        qualification = self.qualification()
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-03T00:00:00+00:00",
        )
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, {"provider": "e" * 64},
            now="2026-08-03T00:00:01+00:00",
        )
        with self.assertRaisesRegex(
            ValueError, "durable production evidence is unavailable"
        ):
            rows = qrels(300, 20, slice_count=30)
            history_audit_store.persist_semantic_qualification(
                self.conn,
                subject.validate_qrels(rows, partitions(rows), scope="real"),
                outputs(rows), policy(), evidence(),
                now="2026-08-03T00:00:02+00:00",
            )
        heads = history_audit_store.current_semantic_dependency_heads(self.conn)
        self.assertEqual(heads["provider"], "e" * 64)

    def test_upgrade_rejects_preexisting_unbound_complete_no_match(self):
        prior = history_store.connect(self.root / "prior.sqlite3")
        history_store.init_schema(prior)
        migrations = history_audit_store.MIGRATIONS
        target = next(
            index for index, migration in enumerate(migrations)
            if migration.component == "semantic-release-authorization"
        )
        with mock.patch.object(history_audit_store, "MIGRATIONS", migrations[:target]):
            history_audit_store.init_schema(prior)
        receipt = self.receipt(receipt_sha="e" * 64)
        self.conn.close()
        self.conn = prior
        self.install_owner(receipt)
        self.direct_insert(receipt)
        with self.assertRaises(history_audit_store.AuditMigrationError):
            history_audit_store.init_schema(self.conn)


class MigrationLedgerHardeningTests(unittest.TestCase):
    def test_matching_sha_preseed_cannot_skip_guard_migration(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            conn.executescript(history_audit_store._LEDGER_SQL)
            migration = next(
                item for item in history_audit_store.MIGRATIONS
                if item.component == "migration-ledger-guard"
            )
            conn.execute(
                "INSERT INTO audit_schema_migrations VALUES(?,?,?,?)",
                (migration.component, migration.version, migration.sha256,
                 "2026-08-03T00:00:00+00:00"),
            )
            conn.commit()
            with self.assertRaises(history_audit_store.AuditMigrationError):
                history_audit_store.init_schema(conn)
            conn.close()

    def test_migration_ledger_is_host_guarded_and_immutable(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(
                    "INSERT INTO audit_schema_migrations VALUES('forged',1,?,?)",
                    ("f" * 64, "2026-08-03T00:00:00+00:00"),
                )
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(
                    "UPDATE audit_schema_migrations SET applied_at='forged'"
                )
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM audit_schema_migrations")
            conn.close()

    def test_public_init_cannot_disable_schema_postconditions(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            with self.assertRaises(TypeError):
                history_audit_store.init_schema(conn, _verify_structure=False)
            conn.close()


if __name__ == "__main__":
    unittest.main()
