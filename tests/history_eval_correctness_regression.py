#!/usr/bin/env python3
"""Regression tests for history evaluation correctness checks."""

import copy
import json
import math
import pathlib
import shutil
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit_eval_v2 as audit_eval
from lib import history_eval
from tests import history_audit_eval_smoke as audit_fixture


BENCHMARK = ROOT / "calib/history-retrieval/synthetic"


def _rewrite_outputs(root, transform):
    for arm in history_eval.ARMS:
        path = root / "outputs" / (arm + ".jsonl")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows = [transform(arm, index, row) for index, row in enumerate(rows)]
        path.write_bytes(b"".join(history_eval.canonical_bytes(row) for row in rows))


class HistoryEvalCorrectnessRegression(unittest.TestCase):
    def benchmark_copy(self):
        temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(temporary.name) / "benchmark"
        shutil.copytree(BENCHMARK, root)
        self.addCleanup(temporary.cleanup)
        return root

    def test_nonfinite_latency_is_rejected(self):
        for nonfinite in (math.nan, math.inf, -math.inf):
            with self.subTest(nonfinite=nonfinite):
                root = self.benchmark_copy()

                def mutate(arm, index, row):
                    if arm == "end-to-end" and index == 0:
                        row["latency_ms"] = nonfinite
                    return row

                _rewrite_outputs(root, mutate)
                with self.assertRaisesRegex(
                    history_eval.BenchmarkError, "output row is invalid"
                ):
                    history_eval.evaluate_benchmark(root, verify_expected=False)

    def test_nonfinite_numeric_capability_evidence_is_rejected(self):
        capability = history_eval.build_synthetic_capability_for_test(
            BENCHMARK
        )["calibration_capability"]
        capability["evaluation_evidence"]["primary_metrics"]["duplicate"][
            "observed"
        ] = math.nan
        with self.assertRaisesRegex(
            history_eval.BenchmarkError, "non-finite"
        ):
            history_eval.validate_capability_artifact(
                capability,
                required_scope=history_eval.SYNTHETIC_SCOPE,
            )

    def test_v2_depth_gate_uses_per_channel_execution_evidence(self):
        root = self.benchmark_copy()
        committed = json.loads(
            (root / "policy-commitment.json").read_text()
        )["selected_depths"]["per_channel_depth"]

        def add_depths(arm, index, row):
            row["schema_version"] = history_eval.OUTPUT_SCHEMA_VERSION
            row["channel_depths"] = {
                "exact": (
                    committed + 1
                    if arm == "end-to-end" and index == 0
                    else 0
                ),
                "fts": 0,
                "dense": 0,
                "lineage": 0,
            }
            return row

        _rewrite_outputs(root, add_depths)
        capability = history_eval.build_synthetic_capability_for_test(root)[
            "calibration_capability"
        ]
        self.assertEqual(
            capability["schema_version"],
            history_eval.CAPABILITY_SCHEMA_VERSION,
        )
        gate = capability["evaluation_evidence"]["selected_depths"][
            "per_channel_depth"
        ]
        self.assertEqual(gate["observed"], committed + 1)
        self.assertFalse(gate["passed"])
        self.assertFalse(
            capability["evaluation_evidence"]["all_gates_passed"]
        )

    def test_legacy_output_and_capability_versions_are_synthetic_only(self):
        bundle = history_eval.build_synthetic_capability_for_test(BENCHMARK)
        capability = bundle["calibration_capability"]
        self.assertEqual(
            capability["schema_version"],
            history_eval.LEGACY_CAPABILITY_SCHEMA_VERSION,
        )
        history_eval.validate_capability_artifact(
            capability, required_scope=history_eval.SYNTHETIC_SCOPE
        )
        with self.assertRaisesRegex(
            history_eval.BenchmarkError,
            "synthetic_contract_only.*production",
        ):
            history_eval.validate_capability_artifact(
                capability, required_scope=history_eval.PRODUCTION_SCOPE
            )

    def test_policy_containers_and_nonfinite_bounds_fail_as_value_errors(self):
        for path, replacement in (
            (("shadow", "critical_slices"), []),
            (("production", "aggregate"), []),
            (("production", "required_slices"), []),
            (
                (
                    "production",
                    "aggregate",
                    "minimum_recall_lower_bound",
                ),
                math.nan,
            ),
        ):
            with self.subTest(path=path):
                policy = audit_fixture.policy()
                target = policy
                for component in path[:-1]:
                    target = target[component]
                target[path[-1]] = replacement
                with self.assertRaises(ValueError):
                    audit_eval.semantic_policy_sha256(policy)

    def test_qrel_elements_fail_before_set_or_sort_operations(self):
        rows = audit_fixture.qrels(1, 0)
        partitions = audit_fixture.partitions(rows)
        partitions["test"] = [[]]
        with self.assertRaises(ValueError):
            audit_eval.validate_qrels(rows, partitions, scope="real")
        partitions = audit_fixture.partitions(rows)
        rows[0]["risk_slices"] = ["low_overlap", []]
        with self.assertRaises(ValueError):
            audit_eval.validate_qrels(rows, partitions, scope="real")
        rows = audit_fixture.qrels(1, 0)
        partitions = audit_fixture.partitions(rows)
        rows[0]["semantic_relation"] = []
        with self.assertRaises(ValueError):
            audit_eval.validate_qrels(rows, partitions, scope="real")

    def test_production_qualification_binds_identities_and_expiry(self):
        rows = audit_fixture.qrels(300, 20, slice_count=30)
        dataset = audit_eval.validate_qrels(
            rows, audit_fixture.partitions(rows), scope="real"
        )
        outputs = audit_fixture.outputs(rows)
        evidence = audit_fixture.evidence()
        evidence["evaluation_hash"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "evaluation identity"):
            audit_eval.evaluate_production_qualification(
                dataset, outputs, audit_fixture.policy(), evidence
            )
        evidence = audit_fixture.evidence()
        evidence["expires_at"] = "2020-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "expired"):
            audit_eval.evaluate_production_qualification(
                dataset, outputs, audit_fixture.policy(), evidence
            )


if __name__ == "__main__":
    unittest.main()
