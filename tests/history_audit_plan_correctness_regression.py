#!/usr/bin/env python3
"""Correctness regressions for history audit planning authority."""

import copy
import hashlib
import json
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_eval_v2 as evaluator
from lib import history_audit_plan as planner
from lib import history_contract_v2 as contract


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_policy(name):
    return json.loads((ROOT / "history" / name).read_text(encoding="utf-8"))


def runtime_plan(*, reverse_records=False):
    candidate_id = "stg-v2-" + sha("correctness-candidate")
    candidate = {
        "candidate_id": candidate_id,
        "candidate_hash": "",
        "raw_artifact_sha": sha("correctness-artifact"),
        "source_order": 0,
    }
    candidate["candidate_hash"] = planner.runtime_candidate_hash(candidate)
    records = [
        {
            "item_id": "asset-1",
            "artifact_sha": sha("frozen record"),
            "content": "frozen record",
            "lineage_id": "lineage-1",
        },
        {
            "item_id": "asset-2",
            "artifact_sha": sha("second frozen record"),
            "content": "second frozen record",
            "lineage_id": "lineage-2",
        },
    ]
    if reverse_records:
        records.reverse()
    run_id = "run-correctness"
    batch_id = "batch-correctness"
    current_ids = [candidate_id]
    expected_ids = ["asset-1", "asset-2"]
    snapshot_material = {
        "run_id": run_id,
        "batch_id": batch_id,
        "history_as_of_watermark": 7,
        "current_batch_id_namespace": "history-v2-staging-v1",
        "current_batch_ids_hash": contract.ordered_set_sha256(
            "history-current-batch-ids-v2", current_ids
        ),
        "exclusion_policy_sha": sha("exclusion"),
        "expected_asset_ids_hash": contract.ordered_set_sha256(
            "history-snapshot-assets-v2", expected_ids
        ),
    }
    snapshot_hash = contract.framed_sha256(
        "history-snapshot-v2", contract.canonical_bytes(snapshot_material)
    )
    snapshot = {
        "snapshot_id": contract.framed_sha256(
            "history-snapshot-id-v2",
            contract.canonical_bytes({
                "run_id": run_id,
                "batch_id": batch_id,
                "snapshot_hash": snapshot_hash,
            }),
        ),
        "snapshot_hash": snapshot_hash,
        "history_as_of_watermark": 7,
        "current_batch_id_namespace": "history-v2-staging-v1",
        "current_batch_ids_hash": snapshot_material["current_batch_ids_hash"],
        "current_batch_ids": current_ids,
        "exclusion_policy_sha": snapshot_material["exclusion_policy_sha"],
        "expected_asset_ids_hash": snapshot_material["expected_asset_ids_hash"],
        "expected_asset_ids": expected_ids,
        "records": records,
    }
    capability = {
        "provider": "fake-provider",
        "capability_profile_hash": sha("capability"),
        "model_identity": "fake-model-v1",
        "reasoning_identity": "high",
        "model_default": False,
        "reasoning_default": False,
        "executable": "fake-provider",
        "cli_revision": "fake-cli-v1",
        "max_output_tokens": 64,
        "output_token_cap_binding": "test-provider-native-exact",
        "output_token_cap_semantics": "reasoning-and-visible-output",
    }
    pools = {
        stage: ["fake-provider"]
        for stage in ("comparator", "map", "detail", "reduce")
    }
    return planner.build_test_only_runtime_plan(
        run_id=run_id,
        batch_id=batch_id,
        snapshot=snapshot,
        candidate=candidate,
        provider_pools_ordered=pools,
        provider_capabilities={"fake-provider": capability},
        intent="duplicate_search",
        matched_router_rule_ids=["test-rule"],
        semantic_policy_profile_id="semantic-test-v1",
        test_execution_binding={
            "schema_version": "history-test-execution-binding-v1",
            "fake_executable_sha256": sha("fake executable"),
            "protocol_revision": "fake-protocol-v1",
        },
        max_output_tokens=64,
    )


class HistoryAuditPlanCorrectnessRegression(unittest.TestCase):
    def test_semantic_policy_bytes_and_hash_bind_host_authority(self):
        policy = load_policy("semantic-release-policy-v1.json")
        authority = planner._host_authority_for_capacity("fake-safe-24k-v1")
        self.assertIsInstance(authority["semantic_policy_canonical_bytes"], bytes)
        self.assertEqual(
            authority["semantic_policy_sha"],
            evaluator.semantic_policy_sha256(policy),
        )
        policies = {
            name: load_policy(name)
            for name in (
                "capacity-profiles-v1.json", "l2-budget-v1.json",
                "risk-policy-v1.json", "settlement-policy-v1.json",
                "semantic-release-policy-v1.json",
            )
        }
        policies["semantic-release-policy-v1.json"]["shadow"][
            "minimum_positive_lineages"
        ] += 1
        with mock.patch.object(
            planner,
            "_load_host_policy",
            side_effect=lambda name: copy.deepcopy(policies[name]),
        ):
            changed = planner._host_authority_for_capacity("fake-safe-24k-v1")
        self.assertNotEqual(changed["semantic_policy_sha"], authority["semantic_policy_sha"])
        self.assertNotEqual(changed["authority_id"], authority["authority_id"])

    def test_rehashed_request_substitution_is_reconstructed_and_rejected(self):
        plan = runtime_plan()
        shard = plan["shards"][0]
        raw = "{}"
        shard.update({
            "serialized_request": raw,
            "request_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "final_request_tokens": len(raw.encode("utf-8")),
        })
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.runtime_plan_sha(plan)
        self.assertEqual(caught.exception.code, "invalid_runtime_shards")

    def test_utilization_above_one_million_is_invalid(self):
        plan = runtime_plan()
        profile = copy.deepcopy(plan["capacity_profile"])
        profile["utilization_ppm"] = 1_000_001
        with self.assertRaises(planner.AuditPlanError):
            planner._validate_authoritative_capacity_profile(
                profile, error_code="invalid_capacity_profile"
            )

    def test_verified_usage_requires_all_usage_counters(self):
        policy = load_policy("l2-budget-v1.json")
        events = []
        reserved = planner.reserve_attempt(
            policy, "duplicate_search", "candidate", "task", "initial",
            {"input_tokens": 1, "output_tokens": 1, "provider_usage_units": 2},
            events,
        )
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.settle_attempt(
                reserved["event_id"], {"input_tokens": 1}, True, events
            )
        self.assertEqual(caught.exception.code, "invalid_settlement")

    def test_currency_budget_without_price_fails_closed(self):
        policy = load_policy("l2-budget-v1.json")
        for scope in ("round", "candidate"):
            policy["intents"]["duplicate_search"][scope]["currency_micros"] = 10
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.reserve_attempt(
                policy, "duplicate_search", "candidate", "task", "initial",
                {"input_tokens": 1, "output_tokens": 1, "provider_usage_units": 2},
                [],
            )
        self.assertEqual(caught.exception.code, "unknown_currency_budget")

    def test_verified_settlement_currency_shape_matches_reservation(self):
        policy = load_policy("l2-budget-v1.json")
        for scope in ("round", "candidate"):
            policy["intents"]["duplicate_search"][scope][
                "currency_micros"
            ] = 100
        events = []
        reserved = planner.reserve_attempt(
            policy,
            "duplicate_search",
            "candidate",
            "task",
            "initial",
            {
                "input_tokens": 1,
                "output_tokens": 1,
                "provider_usage_units": 2,
                "currency_micros": 9,
            },
            events,
        )
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.settle_attempt(
                reserved["event_id"],
                {"input_tokens": 1, "output_tokens": 1,
                 "provider_usage_units": 2},
                True,
                events,
            )
        self.assertEqual(caught.exception.code, "invalid_settlement")
        self.assertEqual(len(events), 1)
        planner.settle_attempt(
            reserved["event_id"],
            {"input_tokens": 1, "output_tokens": 1,
             "provider_usage_units": 2, "currency_micros": 3},
            True,
            events,
        )
        self.assertEqual(
            planner.budget_totals(events, "duplicate_search")["currency_micros"],
            3,
        )

    def test_verified_settlement_rejects_unbudgeted_currency(self):
        events = []
        reserved = planner.reserve_attempt(
            load_policy("l2-budget-v1.json"),
            "duplicate_search",
            "candidate",
            "task",
            "initial",
            {"input_tokens": 1, "output_tokens": 1,
             "provider_usage_units": 2},
            events,
        )
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.settle_attempt(
                reserved["event_id"],
                {"input_tokens": 1, "output_tokens": 1,
                 "provider_usage_units": 2, "currency_micros": 1},
                True,
                events,
            )
        self.assertEqual(caught.exception.code, "invalid_settlement")
        self.assertEqual(len(events), 1)

    def test_forged_extra_frozen_record_is_rejected(self):
        plan = runtime_plan()
        plan["snapshot"]["records"].append({
            "item_id": "asset-extra",
            "artifact_sha": sha("self-consistent extra"),
            "content": "self-consistent extra",
            "lineage_id": "lineage-extra",
        })
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.runtime_plan_sha(plan)
        self.assertEqual(caught.exception.code, "invalid_runtime_snapshot")

    def test_attempt_manifest_requires_frozen_pool_capability_and_ordinal(self):
        plan = runtime_plan()
        capability = copy.deepcopy(plan["provider_capabilities"]["fake-provider"])
        accepted = planner.attempt_manifest(plan, 0, 0, capability)
        self.assertEqual(accepted["provenance"]["provider"], "fake-provider")
        mutations = (
            ("provider", "other-provider"),
            ("capability_profile_hash", sha("other capability")),
            ("model_identity", "other-model"),
            ("reasoning_identity", "low"),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                forged = copy.deepcopy(capability)
                forged[field] = replacement
                with self.assertRaises(planner.AuditPlanError) as caught:
                    planner.attempt_manifest(plan, 0, 0, forged)
                self.assertEqual(caught.exception.code, "invalid_attempt")
        for ordinal in (-1, True, planner.MAX_ATTEMPTS):
            with self.subTest(ordinal=ordinal):
                with self.assertRaises(planner.AuditPlanError) as caught:
                    planner.attempt_manifest(plan, 0, ordinal, capability)
                self.assertEqual(caught.exception.code, "invalid_attempt")
        with mock.patch.object(
            contract,
            "attempt_id",
            side_effect=contract.ContractV2Error("forged provenance"),
        ):
            with self.assertRaises(planner.AuditPlanError) as caught:
                planner.attempt_manifest(plan, 0, 0, capability)
        self.assertEqual(caught.exception.code, "invalid_attempt")

    def test_record_order_is_normalized_before_render_and_storage(self):
        forward = runtime_plan()
        reverse = runtime_plan(reverse_records=True)
        self.assertEqual(forward["plan_sha"], reverse["plan_sha"])
        self.assertEqual(forward["shards"], reverse["shards"])
        self.assertEqual(
            [record["item_id"] for record in reverse["snapshot"]["records"]],
            ["asset-1", "asset-2"],
        )

    def test_request_serializer_revisions_have_exact_distinct_bytes(self):
        value = {"value": "golden"}
        v1 = planner._serialize_request_value(value, "history-audit-request-v1")
        v2 = planner._serialize_request_value(value, "history-audit-request-v2")
        self.assertEqual(v1, b'{"value":"golden"}\n')
        self.assertEqual(v2, b'{"value":"golden"}')
        plan = runtime_plan()
        shard = plan["shards"][0]
        self.assertFalse(shard["serialized_request"].endswith("\n"))
        shard["serialized_request"] += "\n"
        raw = shard["serialized_request"].encode("utf-8")
        shard["request_sha256"] = hashlib.sha256(raw).hexdigest()
        shard["final_request_tokens"] = len(raw)
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.runtime_plan_sha(plan)
        self.assertEqual(caught.exception.code, "invalid_runtime_shards")

    def test_invalid_semantic_host_policy_is_rejected_by_evaluator_authority(self):
        mutations = (
            lambda policy: policy["shadow"].__setitem__(
                "minimum_positive_lineages", "30"
            ),
            lambda policy: policy.__setitem__("unknown", 1),
            lambda policy: policy["production"]["aggregate"].__setitem__(
                "minimum_recall_lower_bound", True
            ),
            lambda policy: policy["shadow"].__setitem__(
                "minimum_positive_lineages", 29
            ),
        )
        base_names = (
            "capacity-profiles-v1.json", "l2-budget-v1.json",
            "risk-policy-v1.json", "settlement-policy-v1.json",
            "semantic-release-policy-v1.json",
        )
        for mutate in mutations:
            policies = {name: load_policy(name) for name in base_names}
            mutate(policies["semantic-release-policy-v1.json"])
            with self.subTest(policy=policies["semantic-release-policy-v1.json"]):
                with mock.patch.object(
                    planner,
                    "_load_host_policy",
                    side_effect=lambda name, values=policies: copy.deepcopy(
                        values[name]
                    ),
                ):
                    with self.assertRaises(planner.AuditPlanError) as caught:
                        planner._host_runtime_authority()
                self.assertEqual(caught.exception.code, "invalid_host_policy")

    def test_negative_shard_index_is_not_python_negative_indexing(self):
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.attempt_manifest(runtime_plan(), -1, 0, {})
        self.assertEqual(caught.exception.code, "invalid_attempt")

    def test_malformed_snapshot_contract_error_is_normalized(self):
        plan = runtime_plan()
        plan["snapshot"]["current_batch_ids"] = [object()]
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.runtime_plan_sha(plan)
        self.assertEqual(caught.exception.code, "invalid_runtime_snapshot")


if __name__ == "__main__":
    unittest.main()
