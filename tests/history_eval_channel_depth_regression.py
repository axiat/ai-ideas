#!/usr/bin/env python3
"""Regression for exact v2 channel-depth capability binding."""

import copy
import json
import pathlib
import shutil
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_eval


BENCHMARK = ROOT / "calib/history-retrieval/synthetic"


class HistoryEvalChannelDepthRegression(unittest.TestCase):
    def benchmark_copy(self, exact_depth):
        temporary = tempfile.TemporaryDirectory()
        root = pathlib.Path(temporary.name) / "benchmark"
        shutil.copytree(BENCHMARK, root)
        self.addCleanup(temporary.cleanup)
        for arm in history_eval.ARMS:
            path = root / "outputs" / (arm + ".jsonl")
            rows = []
            for line in path.read_text().splitlines():
                row = json.loads(line)
                row["schema_version"] = history_eval.OUTPUT_SCHEMA_VERSION
                row["channel_depths"] = {
                    "exact": exact_depth if arm == "end-to-end" else 0,
                    "fts": 1 if arm == "end-to-end" else 0,
                    "dense": 0,
                    "lineage": 0,
                }
                rows.append(row)
            path.write_bytes(
                b"".join(history_eval.canonical_bytes(row) for row in rows)
            )
        return root

    def test_forged_high_channel_depth_rejected_when_actual_is_low(self):
        actual_root = self.benchmark_copy(exact_depth=0)
        forged_root = self.benchmark_copy(exact_depth=1)
        actual = history_eval.build_synthetic_capability_for_test(actual_root)
        forged = history_eval.build_synthetic_capability_for_test(forged_root)

        actual_gate = actual["calibration_capability"]["evaluation_evidence"]
        forged_gate = forged["calibration_capability"]["evaluation_evidence"]
        self.assertEqual(
            actual_gate["selected_depths"]["per_channel_depth"],
            forged_gate["selected_depths"]["per_channel_depth"],
        )
        self.assertNotEqual(
            actual_gate["confidence_intervals_sha256"],
            forged_gate["confidence_intervals_sha256"],
        )

        capability = actual["calibration_capability"]
        capability["evaluation_evidence"] = copy.deepcopy(forged_gate)
        capability.pop("signature")
        capability["canonical_seal_sha256"] = history_eval._capability_seal_sha(
            capability
        )
        trust_root = json.loads(
            (actual_root / "test-witness-key.json").read_text()
        )
        capability["signature"] = history_eval._test_signature(
            history_eval._capability_signature_domain(
                history_eval.CAPABILITY_SCHEMA_VERSION
            ),
            capability,
            trust_root,
        )

        with self.assertRaisesRegex(
            history_eval.BenchmarkError,
            "derived evaluation evidence is invalid",
        ):
            history_eval.validate_calibration_capability(
                actual,
                policy=history_eval.DEFAULT_POLICY,
                trust_root=trust_root,
                required_scope=history_eval.SYNTHETIC_SCOPE,
                benchmark=actual_root,
            )


if __name__ == "__main__":
    unittest.main()
