#!/usr/bin/env python3
import copy
import hashlib
import json
import pathlib
import struct
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_runtime


try:
    from lib import history_contract_v2 as contract
except ImportError:
    class _CurrentCodecAdapter:
        ContractV2Error = ValueError

        canonical_bytes = staticmethod(history_runtime.canonical_bytes)

        @staticmethod
        def parse_json_bytes(raw, *, allowed_fields=None):
            value = json.loads(raw.decode("utf-8"))
            if allowed_fields is not None and set(value) != set(allowed_fields):
                raise ValueError("fields differ")
            return value

        @staticmethod
        def framed_sha256(domain, *parts):
            digest = hashlib.sha256()
            for part in (domain.encode("utf-8"),) + parts:
                digest.update(struct.pack(">Q", len(part)))
                digest.update(part)
            return digest.hexdigest()

        @classmethod
        def ordered_set_sha256(cls, domain, values):
            return cls.framed_sha256(
                domain, cls.canonical_bytes(sorted(values))
            )

        @classmethod
        def plan_sha256(cls, manifest):
            return cls.framed_sha256(
                "history-plan-v2", cls.canonical_bytes(manifest)
            )

        @classmethod
        def logical_task_key(cls, plan_sha, stage, candidate_id, input_id):
            return cls.framed_sha256(
                "history-logical-task-v2",
                bytes.fromhex(plan_sha),
                stage.encode(),
                candidate_id.encode(),
                input_id.encode(),
            )

        @classmethod
        def attempt_id(cls, task_key, ordinal, provenance):
            return cls.framed_sha256(
                "history-attempt-v2",
                bytes.fromhex(task_key),
                ordinal.to_bytes(8, "big"),
                cls.canonical_bytes(provenance),
            )

        @staticmethod
        def validate_receipt(value):
            return value

    contract = _CurrentCodecAdapter()


VECTORS = json.loads(
    (ROOT / "tests/fixtures/history-v2-codec-vectors.json").read_text(
        encoding="utf-8"
    )
)
SHA = "0" * 64


def valid_receipt():
    receipt = {
        "manifest_schema_version": "history-audit-manifest-v2",
        "canonical_codec_version": "history-canonical-json-v2",
        "run_id": "run-1",
        "plan_hash": SHA,
        "candidate_hash": SHA,
        "snapshot_id": "snapshot-1",
        "snapshot_hash": SHA,
        "history_as_of_watermark": 7,
        "current_batch_id_namespace": "history-v2-staging-v1",
        "current_batch_ids_hash": SHA,
        "exclusion_policy_sha": SHA,
        "expected_asset_ids_hash": SHA,
        "observed_asset_ids_hash": SHA,
        "missing_ids": [],
        "duplicate_ids": [],
        "extra_ids": [],
        "invalid_schema": False,
        "invalid_anchor": False,
        "truncated": False,
        "provider_pools_ordered": {
            "comparator": ["codex", "kimi"],
            "map": ["grok", "codex"],
            "detail": ["codex"],
            "reduce": ["codex"],
        },
        "provider_capability_profile_hashes": [SHA],
        "capacity_profile_id": "safe-24k-v1",
        "semantic_policy_profile_id": "shadow-calibration-v1",
        "risk_policy_version": "risk-v1",
        "matched_router_rule_ids": ["flat-baseline"],
        "settlement_policy_sha": SHA,
        "shard_plan_sha": SHA,
        "logical_task_hashes": [SHA],
        "attempt_manifest_hashes": [SHA],
        "raw_request_output_cas_hashes": [SHA],
        "minimum_receipt_sha": SHA,
        "coverage_complete": True,
        "adjudication_complete": True,
        "semantic_policy_qualified": False,
        "no_match_basis": None,
        "final_status": "uncertain",
        "stage_reason_code": "semantic_policy_unqualified",
        "evidence_anchors": [],
    }
    if hasattr(contract, "minimum_receipt_sha"):
        receipt["minimum_receipt_sha"] = contract.minimum_receipt_sha(receipt)
    return receipt


class HistoryContractV2Smoke(unittest.TestCase):
    def test_nfc_equivalent_values_have_identical_canonical_bytes(self):
        self.assertEqual(
            contract.canonical_bytes({"name": "caf\u00e9"}),
            contract.canonical_bytes({"name": "cafe\u0301"}),
        )

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(contract.ContractV2Error):
            contract.parse_json_bytes(b'{"run_id":"a","run_id":"b"}\n')

    def test_ordered_pool_order_changes_plan_sha(self):
        manifest = copy.deepcopy(VECTORS["plan"]["manifest"])
        changed = copy.deepcopy(manifest)
        changed["provider_pools_ordered"]["comparator"].reverse()
        self.assertNotEqual(
            contract.plan_sha256(manifest), contract.plan_sha256(changed)
        )

    def test_id_set_order_does_not_change_set_sha(self):
        vector = VECTORS["ordered_id_set"]
        self.assertEqual(
            contract.ordered_set_sha256(vector["domain"], vector["values"]),
            contract.ordered_set_sha256(
                vector["domain"], list(reversed(vector["values"]))
            ),
        )

    def test_provider_attempt_changes_attempt_id_not_logical_task_key(self):
        vector = VECTORS["attempt"]
        plan_hash = VECTORS["plan"]["expected_hash"]
        task_key = contract.logical_task_key(
            plan_hash,
            vector["stage"],
            vector["staging_candidate_id"],
            vector["input_id"],
        )
        changed = copy.deepcopy(vector["provenance"])
        changed["provider"] = "kimi"
        self.assertEqual(task_key, vector["expected_logical_task_key"])
        self.assertNotEqual(
            contract.attempt_id(task_key, vector["ordinal"], vector["provenance"]),
            contract.attempt_id(task_key, vector["ordinal"], changed),
        )

    def test_committed_vectors_match_literal_bytes_and_hashes(self):
        for vector in VECTORS["canonical_cases"]:
            with self.subTest(vector=vector["name"]):
                raw = contract.canonical_bytes(vector["value"])
                self.assertEqual(raw, vector["canonical_utf8"].encode("utf-8"))
                self.assertEqual(hashlib.sha256(raw).hexdigest(), vector["sha256"])
        plan = VECTORS["plan"]
        self.assertEqual(
            contract.plan_sha256(plan["manifest"]), plan["expected_hash"]
        )
        id_set = VECTORS["ordered_id_set"]
        self.assertEqual(
            contract.ordered_set_sha256(id_set["domain"], id_set["values"]),
            id_set["expected_hash"],
        )

    def test_receipt_requires_canonical_fields_and_rejects_legacy_aliases(self):
        self.assertEqual(contract.validate_receipt(valid_receipt()), valid_receipt())
        for mutation in (
            lambda value: value.pop("snapshot_hash"),
            lambda value: value.update({"basis": "l2_exhaustive"}),
            lambda value: value.update({"excluded_batch_ids_hash": SHA}),
            lambda value: value.update({"map_provider_pool": ["codex"]}),
        ):
            value = valid_receipt()
            mutation(value)
            with self.assertRaises(contract.ContractV2Error):
                contract.validate_receipt(value)

    def test_no_match_basis_is_closed_and_status_dependent(self):
        for invalid in (
            {"final_status": "uncertain", "no_match_basis": "l2_exhaustive"},
            {"final_status": "complete_no_match", "no_match_basis": None},
            {"final_status": "complete_no_match", "no_match_basis": "legacy"},
            {
                "final_status": "complete_no_match",
                "no_match_basis": "l2_exhaustive",
                "coverage_complete": False,
            },
            {
                "final_status": "complete_no_match",
                "no_match_basis": "l2_exhaustive",
                "adjudication_complete": False,
            },
            {
                "final_status": "complete_no_match",
                "no_match_basis": "l2_exhaustive",
                "semantic_policy_qualified": False,
            },
        ):
            value = valid_receipt()
            value.update(invalid)
            with self.subTest(invalid=invalid):
                with self.assertRaises(contract.ContractV2Error):
                    contract.validate_receipt(value)
        for basis in ("l1_calibrated", "l2_exhaustive"):
            value = valid_receipt()
            value.update(
                final_status="complete_no_match",
                no_match_basis=basis,
                semantic_policy_qualified=True,
            )
            value["minimum_receipt_sha"] = contract.minimum_receipt_sha(value)
            self.assertEqual(contract.validate_receipt(value), value)

    def test_complete_no_match_rejects_invalid_or_truncated_evidence(self):
        for fault in ("invalid_schema", "invalid_anchor", "truncated"):
            value = valid_receipt()
            value.update(
                final_status="complete_no_match",
                no_match_basis="l2_exhaustive",
                semantic_policy_qualified=True,
            )
            value[fault] = True
            with self.subTest(fault=fault):
                with self.assertRaises(contract.ContractV2Error):
                    contract.validate_receipt(value)

    def test_coverage_complete_rejects_invalid_or_truncated_evidence(self):
        for fault in ("invalid_schema", "invalid_anchor", "truncated"):
            value = valid_receipt()
            value[fault] = True
            with self.subTest(fault=fault):
                with self.assertRaises(contract.ContractV2Error):
                    contract.validate_receipt(value)

    def test_coverage_complete_rejects_missing_duplicate_or_extra_ids(self):
        for field in ("missing_ids", "duplicate_ids", "extra_ids"):
            value = valid_receipt()
            value[field] = ["asset-1"]
            with self.subTest(field=field):
                with self.assertRaises(contract.ContractV2Error):
                    contract.validate_receipt(value)


if __name__ == "__main__":
    unittest.main()
