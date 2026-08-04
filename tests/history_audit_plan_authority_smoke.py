#!/usr/bin/env python3
"""Authority-bound runtime plan identity tests."""

import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_plan as planner
from lib import history_contract_v2 as contract
from tests import verify_product_contract


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def load_json(relative_path):
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def canonical_sha(domain, value):
    return contract.framed_sha256(domain, contract.canonical_bytes(value))


def runtime_plan():
    capacity_registry = load_json("history/capacity-profiles-v1.json")
    capacity = copy.deepcopy(capacity_registry["profiles"]["fake-safe-24k-v1"])
    budget = load_json("history/l2-budget-v1.json")
    semantic = load_json("history/semantic-release-policy-v1.json")
    risk = load_json("history/risk-policy-v1.json")
    risk_rule_table_sha = canonical_sha(
        "history-risk-rule-table-v1", risk["rules"]
    )
    candidate_id = "stg-v2-" + sha("authority-candidate-id")
    candidate = {
        "candidate_id": candidate_id,
        "candidate_hash": "",
        "raw_artifact_sha": sha("authority-candidate-raw"),
        "source_order": 0,
    }
    candidate["candidate_hash"] = canonical_sha(
        "history-runtime-candidate-v2",
        {
            "candidate_id": candidate_id,
            "raw_artifact_sha": candidate["raw_artifact_sha"],
            "source_order": 0,
        },
    )
    records = [
        {
            "item_id": "asset-1",
            "artifact_sha": sha("authority-record"),
            "content": "authority-record",
            "lineage_id": "lineage-1",
        }
    ]
    expected_ids = ["asset-1"]
    current_ids = [candidate_id]
    snapshot_material = {
        "run_id": "run-authority-smoke",
        "batch_id": "batch-authority-smoke",
        "history_as_of_watermark": 17,
        "current_batch_id_namespace": "history-v2-staging-v1",
        "current_batch_ids_hash": contract.ordered_set_sha256(
            "history-current-batch-ids-v2", current_ids
        ),
        "exclusion_policy_sha": sha("authority-exclusion"),
        "expected_asset_ids_hash": contract.ordered_set_sha256(
            "history-snapshot-assets-v2", expected_ids
        ),
    }
    snapshot_hash = canonical_sha("history-snapshot-v2", snapshot_material)
    snapshot = {
        "snapshot_id": canonical_sha(
            "history-snapshot-id-v2",
            {
                "run_id": snapshot_material["run_id"],
                "batch_id": snapshot_material["batch_id"],
                "snapshot_hash": snapshot_hash,
            },
        ),
        "snapshot_hash": snapshot_hash,
        "history_as_of_watermark": snapshot_material["history_as_of_watermark"],
        "current_batch_id_namespace": snapshot_material[
            "current_batch_id_namespace"
        ],
        "current_batch_ids_hash": snapshot_material["current_batch_ids_hash"],
        "current_batch_ids": current_ids,
        "exclusion_policy_sha": snapshot_material["exclusion_policy_sha"],
        "expected_asset_ids_hash": snapshot_material["expected_asset_ids_hash"],
        "expected_asset_ids": expected_ids,
        "records": records,
    }
    serialized_request = contract.canonical_bytes(
        {
            "candidate": candidate,
            "items": expected_ids,
            "output_schema": capacity["schema"],
            "prompt": capacity["prompt"],
        }
    ).decode("utf-8")[:-1]
    shards = [
        {
            "shard_id": "map-0000",
            "item_ids": expected_ids,
            "request_sha256": hashlib.sha256(
                serialized_request.encode("utf-8")
            ).hexdigest(),
            "serialized_request": serialized_request,
            "final_request_tokens": len(serialized_request.encode("utf-8")),
        }
    ]
    capability = {
        "provider": "codex",
        "capability_profile_hash": capacity["provider_bindings"]["codex"][
            "capability_profile_hash"
        ],
        "model_identity": capacity["provider_bindings"]["codex"][
            "model_identity"
        ],
        "reasoning_identity": capacity["provider_bindings"]["codex"][
            "reasoning_identity"
        ],
        "model_default": False,
        "reasoning_default": False,
        "executable": "codex",
        "cli_revision": capacity["provider_bindings"]["codex"][
            "cli_revision"
        ],
    }
    return {
        "schema_version": "history-audit-plan-v2",
        "run_id": snapshot_material["run_id"],
        "batch_id": snapshot_material["batch_id"],
        "candidate": candidate,
        "snapshot": snapshot,
        "provider_pools_ordered": {
            stage: ["codex"]
            for stage in ("comparator", "map", "detail", "reduce")
        },
        "provider_capability_profile_hashes": {
            "codex": capability["capability_profile_hash"]
        },
        "provider_capabilities": {"codex": capability},
        "capacity_profile_id": capacity["profile_id"],
        "base_capacity_profile_id": capacity["base_profile_id"],
        "semantic_policy_profile_id": semantic["semantic_policy_profile_id"],
        "risk_policy_version": "%s@%s" % (
            risk["risk_policy_version"], risk_rule_table_sha
        ),
        "matched_router_rule_ids": ["retriever_uncalibrated"],
        "settlement_policy_sha": budget["settlement_policy_sha"],
        "risk_policy_sha": budget["risk_policy_sha"],
        "capacity_profile": capacity,
        "budget_policy": budget,
        "shards": shards,
        "intent": "duplicate_search",
    }


class HistoryAuditPlanAuthoritySmoke(unittest.TestCase):
    def assert_rejected(self, plan, expected_code):
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.runtime_plan_sha(plan)
        self.assertEqual(caught.exception.code, expected_code)

    def test_receipt_facing_prompt_and_schema_identities_change_plan_or_reject(self):
        plan = runtime_plan()
        baseline = planner.runtime_plan_sha(plan)
        mutations = (
            ("capacity_profile_id", "other-capacity-v1"),
            ("base_capacity_profile_id", "other-base-v1"),
            ("semantic_policy_profile_id", "other-semantic-v1"),
            ("risk_policy_version", "other-risk-v1"),
            ("matched_router_rule_ids", []),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                changed = copy.deepcopy(plan)
                changed[field] = replacement
                try:
                    changed_sha = planner.runtime_plan_sha(changed)
                except planner.AuditPlanError:
                    continue
                self.assertNotEqual(
                    changed_sha,
                    baseline,
                    f"runtime plan ignored receipt-facing identity {field}",
                )

        for field in ("prompt", "schema"):
            with self.subTest(field=field):
                changed = copy.deepcopy(plan)
                changed["capacity_profile"][field]["text"] += "-changed"
                changed["capacity_profile"][field]["sha256"] = hashlib.sha256(
                    changed["capacity_profile"][field]["text"].encode("utf-8")
                ).hexdigest()
                try:
                    changed_sha = planner.runtime_plan_sha(changed)
                except planner.AuditPlanError:
                    continue
                self.assertNotEqual(changed_sha, baseline)

    def test_same_capacity_profile_id_cannot_authorize_changed_content(self):
        for mutation in ("item_cap", "provider_binding", "extra_field"):
            with self.subTest(mutation=mutation):
                changed = runtime_plan()
                if mutation == "item_cap":
                    changed["capacity_profile"]["item_cap"] += 1
                elif mutation == "provider_binding":
                    changed["capacity_profile"]["provider_bindings"]["codex"][
                        "evidence_limit_tokens"
                    ] -= 1
                else:
                    changed["capacity_profile"]["caller_extension"] = True
                self.assert_rejected(changed, "unauthorized_capacity_profile")

    def test_same_budget_policy_version_cannot_authorize_changed_ceiling(self):
        changed = runtime_plan()
        changed["budget_policy"]["intents"]["duplicate_search"]["round"][
            "candidates"
        ] = 10**9
        self.assert_rejected(changed, "unauthorized_budget_policy")

    def test_arbitrary_settlement_sha_is_not_host_authority(self):
        changed = runtime_plan()
        changed["settlement_policy_sha"] = "f" * 64
        changed["budget_policy"]["settlement_policy_sha"] = "f" * 64
        self.assert_rejected(changed, "unauthorized_settlement_policy")

    def test_fake_capacity_authority_is_explicit_and_cannot_claim_production(self):
        plan = runtime_plan()
        material = planner.build_runtime_plan_material(plan)
        self.assertEqual(material["authority_scope"], "test-only-shadow")

        caller_scope = copy.deepcopy(plan)
        caller_scope["authority_scope"] = "production"
        self.assertEqual(
            planner.build_runtime_plan_material(caller_scope)["authority_scope"],
            "test-only-shadow",
        )

        forged_material = copy.deepcopy(material)
        forged_material["authority_scope"] = "production"
        with self.assertRaises(planner.AuditPlanError) as caught:
            planner.validate_runtime_plan_material(forged_material)
        self.assertEqual(caught.exception.code, "unauthorized_capacity_profile")

        forged = copy.deepcopy(plan)
        forged["capacity_profile"]["status"] = "hard-complete"
        forged["authority_scope"] = "production"
        self.assert_rejected(forged, "unauthorized_capacity_profile")

    def test_private_test_issuer_allows_only_narrow_fake_shadow_authority(self):
        issue = getattr(planner, "_issue_test_runtime_authority", None)
        self.assertTrue(callable(issue), "missing private test authority issuer")
        base = runtime_plan()
        issued = issue(
            provider_pools_ordered=base["provider_pools_ordered"],
            provider_capabilities=base["provider_capabilities"],
            intent="duplicate_search",
            started_attempt_limit=1,
            semantic_policy_profile_id="semantic-test-v1",
            matched_router_rule_ids=["test-rule-l2"],
        )
        self.assertEqual(issued["authority_scope"], "test-only-shadow")
        self.assertEqual(
            issued["budget_policy"]["intents"]["duplicate_search"]["round"][
                "started_attempts"
            ],
            1,
        )
        plan = copy.deepcopy(base)
        plan.update(copy.deepcopy(issued))
        self.assertEqual(
            planner.build_runtime_plan_material(plan)["authority_scope"],
            "test-only-shadow",
        )

        unissued = copy.deepcopy(plan)
        unissued.pop("authority_id")
        self.assert_rejected(unissued, "unauthorized_capacity_profile")

        with self.assertRaises(planner.AuditPlanError) as caught:
            issue(
                provider_pools_ordered=base["provider_pools_ordered"],
                provider_capabilities=base["provider_capabilities"],
                intent="duplicate_search",
                started_attempt_limit=10**9,
                semantic_policy_profile_id="semantic-test-v1",
                matched_router_rule_ids=["test-rule-l2"],
            )
        self.assertEqual(caught.exception.code, "invalid_test_authority")

    def test_private_test_authority_replays_in_a_fresh_process(self):
        plan = runtime_plan()
        issued = planner._issue_test_runtime_authority(
            provider_pools_ordered=plan["provider_pools_ordered"],
            provider_capabilities=plan["provider_capabilities"],
            intent="duplicate_search",
            started_attempt_limit=4,
            semantic_policy_profile_id="semantic-test-v1",
            matched_router_rule_ids=["retriever_uncalibrated"],
        )
        plan.update(copy.deepcopy(issued))
        material = planner.build_runtime_plan_material(plan)
        expected_sha = planner.runtime_plan_sha_from_material(material)
        program = """
import json
import sys
from lib import history_audit_plan as planner

material = json.load(sys.stdin)
validated = planner.validate_runtime_plan_material(material)
print(json.dumps({
    "authority_scope": validated["authority_scope"],
    "plan_sha": planner.runtime_plan_sha_from_material(validated),
}, sort_keys=True))
"""
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=ROOT,
            input=json.dumps(material),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "authority_scope": "test-only-shadow",
                "plan_sha": expected_sha,
            },
        )
        mutations = {
            "authority": lambda value: value.__setitem__(
                "authority_id", "f" * 64
            ),
            "capacity": lambda value: value["capacity_profile"].__setitem__(
                "max_output_tokens",
                value["capacity_profile"]["max_output_tokens"] + 1,
            ),
            "provider": lambda value: value["provider_capabilities"][
                "codex"
            ].__setitem__("model_identity", "fake-other-model"),
            "budget": lambda value: value["budget_policy"]["intents"][
                "duplicate_search"
            ]["round"].__setitem__("started_attempts", 3),
            "semantic": lambda value: value.__setitem__(
                "semantic_policy_profile_id", "semantic-test-other"
            ),
            "router_rules": lambda value: value.__setitem__(
                "matched_router_rule_ids", []
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(tamper=label):
                changed = copy.deepcopy(material)
                mutate(changed)
                rejected = subprocess.run(
                    [sys.executable, "-c", program],
                    cwd=ROOT,
                    input=json.dumps(changed),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertNotEqual(rejected.returncode, 0)

    def test_host_capacity_binds_executable_and_default_markers(self):
        for field, replacement in (
            ("executable", "other-executable"),
            ("model_default", True),
            ("reasoning_default", True),
        ):
            with self.subTest(field=field):
                changed = runtime_plan()
                changed["provider_capabilities"]["codex"][field] = replacement
                self.assert_rejected(changed, "stale_capacity")

    def test_public_runtime_plan_requires_exact_schema_version(self):
        for replacement in (None, "history-audit-plan-v999"):
            with self.subTest(schema_version=replacement):
                changed = runtime_plan()
                if replacement is None:
                    changed.pop("schema_version")
                else:
                    changed["schema_version"] = replacement
                self.assert_rejected(changed, "invalid_runtime_plan")

    def test_host_capacity_artifact_requires_complete_structural_evidence(self):
        host_artifacts = {
            name: load_json("history/" + name)
            for name in (
                "capacity-profiles-v1.json",
                "l2-budget-v1.json",
                "risk-policy-v1.json",
                "settlement-policy-v1.json",
                "semantic-release-policy-v1.json",
            )
        }
        mutations = {
            "counter": lambda profile: profile.__setitem__(
                "counter", {"kind": "guess", "revision": ""}
            ),
            "usage_source": lambda profile: profile.__setitem__(
                "usage_source", ""
            ),
            "expires_at": lambda profile: profile.__setitem__(
                "expires_at", "2000-01-01T00:00:00+00:00"
            ),
            "evidence_limit_tokens": lambda profile: profile.__setitem__(
                "evidence_limit_tokens", profile["context_tokens"] + 1
            ),
            "immutable_capacity_identity": lambda profile: profile[
                "provider_bindings"
            ]["codex"].__setitem__("immutable_capacity_identity", ""),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                artifacts = copy.deepcopy(host_artifacts)
                profile = artifacts["capacity-profiles-v1.json"]["profiles"][
                    "fake-safe-24k-v1"
                ]
                mutate(profile)
                plan = runtime_plan()
                plan["capacity_profile"] = copy.deepcopy(profile)

                def load_policy(filename):
                    return copy.deepcopy(artifacts[filename])

                with mock.patch.object(
                    planner, "_load_host_policy", side_effect=load_policy
                ):
                    self.assert_rejected(plan, "invalid_host_policy")

    def test_registered_real_capacity_remains_unbudgetable(self):
        changed = runtime_plan()
        real_profile = load_json("history/capacity-profiles-v1.json")["profiles"][
            "safe-24k-v1"
        ]
        changed["capacity_profile_id"] = real_profile["profile_id"]
        changed["capacity_profile"] = real_profile
        changed["base_capacity_profile_id"] = real_profile.get("base_profile_id")
        self.assert_rejected(changed, "unbudgetable_provider")

    def test_settlement_policy_is_a_required_runtime_product_artifact(self):
        self.assertIn(
            "history/settlement-policy-v1.json",
            verify_product_contract.RUNTIME_FILES,
        )

    def test_plan_risk_version_binds_canonical_rule_table_sha(self):
        plan = runtime_plan()
        material = planner.build_runtime_plan_material(plan)
        risk = load_json("history/risk-policy-v1.json")
        rule_table_sha = canonical_sha(
            "history-risk-rule-table-v1", risk["rules"]
        )
        self.assertEqual(
            material["risk_policy_version"],
            f"risk-policy-v1@{rule_table_sha}",
        )
        raw_version = copy.deepcopy(plan)
        raw_version["risk_policy_version"] = "risk-policy-v1"
        self.assert_rejected(raw_version, "unauthorized_risk_policy")

    def test_valid_runtime_shard_binds_recomputed_final_request_tokens(self):
        plan = runtime_plan()
        plan["shards"][0]["final_request_tokens"] = len(
            plan["shards"][0]["serialized_request"].encode("utf-8")
        )
        try:
            planner.runtime_plan_sha(plan)
        except planner.AuditPlanError as exc:
            self.fail(f"valid measured runtime shard rejected: {exc.code}")

    def test_runtime_shard_rejects_rehashed_oversize_serialized_bytes(self):
        plan = runtime_plan()
        serialized = "x" * 12289
        plan["shards"][0].update(
            {
                "serialized_request": serialized,
                "request_sha256": hashlib.sha256(
                    serialized.encode("utf-8")
                ).hexdigest(),
                "final_request_tokens": len(serialized.encode("utf-8")),
            }
        )
        self.assert_rejected(plan, "runtime_capacity_exceeded")

    def test_runtime_shard_rejects_false_final_request_token_count(self):
        plan = runtime_plan()
        plan["shards"][0]["final_request_tokens"] += 1
        self.assert_rejected(plan, "invalid_runtime_shards")

    def test_runtime_shard_reservation_must_fit_bound_intent_budget(self):
        for field in (
            "started_attempts", "input_tokens", "output_tokens",
            "provider_usage_units",
        ):
            with self.subTest(field=field):
                plan = runtime_plan()
                issued = planner._issue_test_runtime_authority(
                    provider_pools_ordered=plan["provider_pools_ordered"],
                    provider_capabilities=plan["provider_capabilities"],
                    intent="duplicate_search",
                    budget_limits={field: 0},
                    semantic_policy_profile_id="semantic-test-v1",
                    matched_router_rule_ids=["test-rule-l2"],
                )
                plan.update(issued)
                self.assert_rejected(plan, "runtime_budget_exceeded")


if __name__ == "__main__":
    unittest.main()
