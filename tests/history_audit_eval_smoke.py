#!/usr/bin/env python3
"""Behavioral smoke tests for semantic release, routing, and cost accounting."""

import copy
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from lib import history_audit
from lib import history_audit_store
from lib import history_store

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


def evidence(*, basis="l2_exhaustive"):
    dependencies = {
        "semantic_policy": subject.semantic_policy_sha256(policy()) if subject else "1" * 64,
        "prompt": "2" * 64,
        "schema": "3" * 64,
        "ordered_provider_pools": "4" * 64,
        "capacity": "5" * 64,
        "provider": "6" * 64,
        "fault": "7" * 64,
        "replay": "8" * 64,
    }
    return {
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
            outputs(rows), policy(), evidence()
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
            self.validated(under), outputs(under), policy(), evidence()
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

    def test_missing_bad_slice_abstains_and_vetoes(self):
        rows = qrels(300, 20, slice_count=29)
        result = self.api("evaluate_production_qualification")(
            self.validated(rows), outputs(rows), policy(), evidence()
        )
        self.assertFalse(result["production_qualified"])
        self.assertEqual(result["metrics"]["slices"]["low_overlap"]["state"], "abstain")
        self.assertIn("slice_low_overlap_abstain", result["vetoes"])

    def test_qualification_persists_exact_dependencies_and_stays_invalidated(self):
        with tempfile.TemporaryDirectory() as root:
            conn = history_store.connect(pathlib.Path(root) / "history.sqlite3")
            history_store.init_schema(conn)
            history_audit_store.init_schema(conn)
            rows = qrels(300, 20, slice_count=30)
            qualification = self.api("evaluate_production_qualification")(
                self.validated(rows), outputs(rows), policy(), evidence()
            )
            stored = history_audit_store.persist_semantic_qualification(
                conn, qualification, now="2026-08-03T00:00:00+00:00"
            )
            found = history_audit_store.lookup_semantic_qualification(
                conn,
                semantic_policy_profile_id=qualification["semantic_policy_profile_id"],
                no_match_basis="l2_exhaustive",
                policy_sha256=qualification["policy_sha256"],
                corpus_snapshot_hash=qualification["corpus_snapshot_hash"],
                evaluation_hash=qualification["evaluation_hash"],
                dependency_hashes=qualification["dependency_hashes"],
                now="2026-08-03T00:00:01+00:00",
            )
            self.assertEqual(found["qualification_id"], stored["qualification_id"])
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

    def test_cost_counts_every_attempt_and_omits_unknown_currency(self):
        ledger = {
            "attempt_events": [
                {"event_id": "s1", "event_type": "attempt_started", "attempt_id": "a1", "intent": "hunt", "candidate_id": "c1", "stage": "l1", "attempt_kind": "initial"},
                {"event_id": "s2", "event_type": "attempt_started", "attempt_id": "a2", "intent": "hunt", "candidate_id": "c1", "stage": "l2", "attempt_kind": "retry"},
                {"event_id": "s3", "event_type": "attempt_started", "attempt_id": "a3", "intent": "hunt", "candidate_id": "c1", "stage": "l2", "attempt_kind": "split"},
                {"event_id": "s4", "event_type": "attempt_started", "attempt_id": "a4", "intent": "hunt", "candidate_id": "c2", "stage": "l1", "attempt_kind": "detail"},
                {"event_id": "s5", "event_type": "attempt_started", "attempt_id": "a5", "intent": "hunt", "candidate_id": "c2", "stage": "l2", "attempt_kind": "reduce"},
                {"event_id": "s6", "event_type": "attempt_started", "attempt_id": "a6", "intent": "hunt", "candidate_id": "c2", "stage": "l2", "attempt_kind": "failover"},
            ],
            "budget_events": [
                {"event_id": "r" + str(i), "event_type": "attempt_reserved", "attempt_id": "a" + str(i), "reserved": {"input_tokens": 10, "output_tokens": 2, "provider_usage_units": 1}}
                for i in range(1, 7)
            ],
            "settlement_events": [
                {"event_id": "x1", "event_type": "attempt_settled", "attempt_id": "a1", "outcome": "failed", "billable": True, "usage_verified": True, "actual": {"input_tokens": 10, "output_tokens": 1, "cache_tokens": 3, "provider_usage_units": 1}, "queue_latency_ms": 2, "run_latency_ms": 3},
                {"event_id": "x2", "event_type": "attempt_settled", "attempt_id": "a2", "outcome": "failed", "billable": True, "usage_verified": False, "actual": None, "queue_latency_ms": 4, "run_latency_ms": 5},
                {"event_id": "x3", "event_type": "attempt_settled", "attempt_id": "a3", "outcome": "success", "billable": True, "usage_verified": True, "actual": {"input_tokens": 8, "output_tokens": 2, "cache_tokens": 0, "provider_usage_units": 1}, "queue_latency_ms": 1, "run_latency_ms": 7},
                {"event_id": "x4", "event_type": "attempt_settled", "attempt_id": "a4", "outcome": "cancelled", "billable": True, "usage_verified": True, "actual": {"input_tokens": 1, "output_tokens": 0, "cache_tokens": 0, "provider_usage_units": 1}, "queue_latency_ms": 1, "run_latency_ms": 1},
                {"event_id": "x5", "event_type": "attempt_settled", "attempt_id": "a5", "outcome": "success", "billable": True, "usage_verified": True, "actual": {"input_tokens": 5, "output_tokens": 2, "cache_tokens": 1, "provider_usage_units": 1, "currency_micros": 9}, "price_source": "verified-price-v1", "queue_latency_ms": 2, "run_latency_ms": 4},
                {"event_id": "x6", "event_type": "attempt_settled", "attempt_id": "a6", "outcome": "failed", "billable": True, "usage_verified": True, "actual": {"input_tokens": 3, "output_tokens": 0, "cache_tokens": 0, "provider_usage_units": 1}, "queue_latency_ms": 2, "run_latency_ms": 2},
            ],
        }
        result = self.api("summarize_realized_cost")(ledger, [])
        hunt = result["intents"]["hunt"]
        self.assertEqual(hunt["realized"]["calls"], 6)
        self.assertEqual(hunt["realized"]["failed_calls"], 3)
        self.assertEqual(hunt["realized"]["billable_cancelled_calls"], 1)
        self.assertNotIn("currency_micros", hunt["realized"])
        self.assertEqual(hunt["escalation_rate"], 1.0)
        self.assertEqual(hunt["expected_per_candidate"]["formula"], "L1 + escalation_rate * L2")


if __name__ == "__main__":
    unittest.main()
