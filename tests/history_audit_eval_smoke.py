#!/usr/bin/env python3
"""Behavioral smoke tests for semantic release, routing, and cost accounting."""

import copy
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


def evidence(*, basis="l2_exhaustive"):
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
            history_audit_store.publish_semantic_dependency_heads(
                conn, qualification["dependency_hashes"],
                now="2026-08-02T23:59:59+00:00",
            )
            stored = history_audit_store.persist_semantic_qualification(
                conn, self.validated(rows), outputs(rows), policy(), evidence(),
                now="2026-08-03T00:00:00+00:00",
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

    def install_l2_execution(self):
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
        history_execution.persist_plan(
            self.conn, plan, route_authority=helper._route_authority(plan)
        )
        output = helper._output(plan)

        def provider(*_):
            return {
                "kind": "success", "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }

        ready_at = self.conn.execute(
            "SELECT created_at FROM audit_logical_tasks WHERE task_hash=?",
            (plan["logical_task_keys"][0],),
        ).fetchone()[0]
        history_execution.run_map_task(
            self.conn, self.root / "cas", plan, plan["logical_task_keys"][0],
            provider, now=ready_at,
        )
        profile_hashes = sorted(plan["provider_capability_profile_hashes"].values())
        self.conn.execute(
            "INSERT INTO audit_capacity_profiles VALUES(?,?,?,?)",
            (plan["capacity_profile_id"], "5" * 64, "{}",
             "2026-08-02T23:59:58+00:00"),
        )
        for provider_name, profile_hash in plan[
            "provider_capability_profile_hashes"
        ].items():
            self.conn.execute(
                "INSERT INTO audit_provider_profiles VALUES(?,?,?,?)",
                (profile_hash, provider_name, "{}",
                 "2026-08-02T23:59:58+00:00"),
            )
        self.conn.commit()
        dependencies = evidence()["dependency_hashes"]
        dependencies.update({
            "plan": plan["plan_sha"],
            "ordered_provider_pools": contract.framed_sha256(
                "history-provider-pools-v2",
                contract.canonical_bytes(plan["provider_pools_ordered"]),
            ),
            "capacity": "5" * 64,
            "provider": contract.framed_sha256(
                "history-provider-capabilities-v2",
                contract.canonical_bytes(profile_hashes),
            ),
        })
        evidence_value = evidence()
        evidence_value["corpus_snapshot_hash"] = plan["snapshot"]["snapshot_hash"]
        evidence_value["dependency_hashes"] = dependencies
        receipt = self.receipt(
            snapshot_hash=plan["snapshot"]["snapshot_hash"]
        )
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
            "risk_policy_version": plan["risk_policy_version"],
            "matched_router_rule_ids": plan["matched_router_rule_ids"],
            "settlement_policy_sha": plan["settlement_policy_sha"],
            "shard_plan_sha": plan["shard_plan_sha"],
            "logical_task_hashes": sorted(plan["logical_task_keys"]),
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
                        ValueError, "production_runtime_authority_unavailable"
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

    def test_production_receipt_vetoes_without_runtime_authority(self):
        _, evidence_value, receipt = self.install_l2_execution()
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        self.persist_qualification(
            now="2026-08-03T00:00:00+00:00", evidence_value=evidence_value
        )
        with self.assertRaises(history_cas.CASError) as caught:
            history_cas.write_minimum_receipt(
                self.conn, receipt, release_context=self.context(qualification),
                now="2026-08-03T00:00:01+00:00",
            )
        self.assertEqual(
            str(caught.exception.__cause__),
            "production_runtime_authority_unavailable",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_semantic_release_authorizations_v2"
            ).fetchone()[0], 0,
        )

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

    def test_dependency_head_change_vetoes_old_qualification_without_profile_revival(self):
        _, evidence_value, receipt = self.install_l2_execution()
        qualification = self.qualification(evidence_value)
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-02T23:59:59+00:00",
        )
        self.persist_qualification(
            now="2026-08-03T00:00:00+00:00", evidence_value=evidence_value
        )
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
        self.persist_qualification(
            now="2026-08-03T00:00:00+00:00", evidence_value=evidence_value
        )
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
        self.persist_qualification(
            now="2026-08-03T00:00:00+00:00", evidence_value=evidence_value
        )
        forged = copy.deepcopy(receipt)
        forged["logical_task_hashes"] = []
        forged["attempt_manifest_hashes"] = []
        forged["raw_request_output_cas_hashes"] = []
        with self.assertRaises(history_cas.CASError):
            history_cas.write_minimum_receipt(
                self.conn, forged, release_context=self.context(qualification),
                now="2026-08-03T00:00:01+00:00",
            )

    def test_stale_qualification_cannot_publish_old_heads_or_revive(self):
        qualification = self.qualification()
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, qualification["dependency_hashes"],
            now="2026-08-03T00:00:00+00:00",
        )
        history_audit_store.publish_semantic_dependency_heads(
            self.conn, {"provider": "e" * 64},
            now="2026-08-03T00:00:01+00:00",
        )
        with self.assertRaises(ValueError):
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
