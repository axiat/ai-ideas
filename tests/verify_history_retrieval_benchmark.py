#!/usr/bin/env python3
"""Offline verifier for the history-retrieval benchmark contract."""

import copy
import json
import pathlib
import shutil
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK = ROOT / "calib/history-retrieval/synthetic"
REQUESTED_BENCHMARK = (
    pathlib.Path(sys.argv[1]).resolve()
    if len(sys.argv) == 2 and not sys.argv[1].startswith("-")
    else DEFAULT_BENCHMARK
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import history_eval


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path, value):
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _write_jsonl(path, values):
    path.write_text(
        "".join(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for value in values
        ),
        encoding="utf-8",
    )


class HistoryRetrievalBenchmarkTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.benchmark = REQUESTED_BENCHMARK

    def copied_benchmark(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="history-retrieval-benchmark."
        )
        destination = pathlib.Path(temporary.name) / "synthetic"
        shutil.copytree(self.benchmark, destination)
        self.addCleanup(temporary.cleanup)
        return destination

    def assert_rejected(self, fragment, mutate):
        benchmark = self.copied_benchmark()
        mutate(benchmark)
        with self.assertRaisesRegex(
            history_eval.BenchmarkError, fragment
        ):
            history_eval.evaluate_benchmark(benchmark)

    def test_synthetic_metrics_match_hand_checked_fixture(self):
        result = history_eval.evaluate_benchmark(self.benchmark)
        expected = _read_json(
            self.benchmark / "expected-metrics.json"
        )
        self.assertEqual(result["scope"], "synthetic_contract_only")
        self.assertEqual(
            set(result["arms"]),
            {
                "retrieval-only",
                "comparator-only",
                "end-to-end",
                "closed-book",
            },
        )
        self.assertEqual(
            result["metrics"],
            expected["metrics"],
        )
        self.assertEqual(
            result["paired_bootstrap"],
            expected["paired_bootstrap"],
        )
        self.assertEqual(expected["tolerance"], 1e-12)
        self.assertEqual(result["paired_bootstrap"]["seed"], 20260723)
        self.assertTrue(result["input_sha256s"])

    def test_future_record_is_not_visible_to_as_of_query(self):
        def mutate(root):
            path = root / "outputs/retrieval-only.jsonl"
            rows = _read_jsonl(path)
            row = next(
                item for item in rows if item["query_id"] == "q-dup-hit"
            )
            row["ranked_record_ids"][0] = "r-future"
            _write_jsonl(path, rows)

        self.assert_rejected("future record", mutate)

    def test_cutoff_ties_require_stable_record_order(self):
        def mutate(root):
            path = root / "outputs/retrieval-only.jsonl"
            rows = _read_jsonl(path)
            row = next(
                item for item in rows if item["query_id"] == "q-dup-hit"
            )
            row["ranked_record_ids"][-2:] = [
                "r-tie-b",
                "r-tie-a",
            ]
            _write_jsonl(path, rows)

        self.assert_rejected("tied ranks.*stable", mutate)

    def test_lineage_cannot_cross_folds(self):
        def mutate(root):
            path = root / "folds.json"
            value = _read_json(path)
            value["folds"]["train"]["lineage_ids"].append(
                value["folds"]["test"]["lineage_ids"][0]
            )
            value["folds"]["train"]["lineage_ids"].sort()
            _write_json(path, value)

        self.assert_rejected("lineage appears in multiple folds", mutate)

    def test_each_relation_set_has_a_no_hit_query(self):
        def mutate(root):
            path = root / "queries.jsonl"
            rows = _read_jsonl(path)
            row = next(
                item for item in rows if item["query_id"] == "q-dup-none"
            )
            row["relation_set"] = "lineage"
            _write_jsonl(path, rows)

        self.assert_rejected("missing no-hit query for duplicate", mutate)

    def test_future_reason_cannot_leak_into_query_text(self):
        def mutate(root):
            corpus = _read_jsonl(root / "corpus.jsonl")
            future = next(
                item for item in corpus if item["record_id"] == "r-future"
            )
            path = root / "queries.jsonl"
            rows = _read_jsonl(path)
            rows[0]["text"] += " " + future["reason"]
            _write_jsonl(path, rows)

        self.assert_rejected("query text leaks", mutate)

    def test_second_independent_adjudication_is_required(self):
        def mutate(root):
            path = root / "adjudications.jsonl"
            rows = _read_jsonl(path)
            rows[0]["judgments"] = rows[0]["judgments"][:1]
            _write_jsonl(path, rows)

        self.assert_rejected("two independent judgments", mutate)

    def test_disagreement_requires_third_adjudication(self):
        def mutate(root):
            path = root / "adjudications.jsonl"
            rows = _read_jsonl(path)
            row = next(
                item
                for item in rows
                if item["query_id"] == "q-fail-hit"
                and item["record_id"] == "r-fail-related"
            )
            row["judgments"] = row["judgments"][:2]
            row["judgments"][1]["relation"] = "unrelated"
            _write_jsonl(path, rows)

        self.assert_rejected("third adjudication", mutate)

    def test_oracle_pack_must_contain_every_gold_id(self):
        def mutate(root):
            path = root / "oracle-packs.jsonl"
            rows = _read_jsonl(path)
            row = next(
                item
                for item in rows
                if item["pack_id"] == "oracle-q-lin-hit"
            )
            row["record_ids"].remove("r-lin-parent")
            _write_jsonl(path, rows)

        self.assert_rejected("oracle pack omits gold", mutate)

    def test_each_query_has_exactly_one_oracle_pack(self):
        def mutate(root):
            path = root / "oracle-packs.jsonl"
            rows = _read_jsonl(path)
            duplicate = copy.deepcopy(
                next(
                    item
                    for item in rows
                    if item["pack_id"] == "oracle-q-lin-hit"
                )
            )
            duplicate["pack_id"] = "oracle-q-lin-hit-copy"
            rows.append(duplicate)
            _write_jsonl(path, rows)

        self.assert_rejected("exactly one oracle pack", mutate)

    def test_unjudged_pair_cannot_be_labeled_negative(self):
        def mutate(root):
            path = root / "outputs/end-to-end.jsonl"
            rows = _read_jsonl(path)
            row = next(
                item for item in rows if item["query_id"] == "q-dup-hit"
            )
            pair = next(
                item
                for item in row["pair_relations"]
                if item["record_id"] == "r-unjudged"
            )
            pair["relation"] = "unrelated"
            _write_jsonl(path, rows)

        self.assert_rejected("unjudged pair.*negative", mutate)

    def test_commitment_cannot_contain_heldout_material(self):
        for forbidden in (
            "heldout_qrels",
            "heldout_adjudications",
            "heldout_outputs",
            "heldout_metrics",
        ):
            with self.subTest(forbidden=forbidden):
                def mutate(root, field=forbidden):
                    path = root / "policy-commitment.json"
                    value = _read_json(path)
                    value[field] = {}
                    _write_json(path, value)

                self.assert_rejected(
                    "policy commitment.*closed|held-out material",
                    mutate,
                )

    def test_output_must_bind_original_commitment(self):
        def mutate(root):
            path = root / "outputs/end-to-end.jsonl"
            rows = _read_jsonl(path)
            rows[0]["policy_commitment_sha256"] = "0" * 64
            _write_jsonl(path, rows)

        self.assert_rejected("policy commitment SHA", mutate)

    def test_commitment_threshold_mutation_is_rejected(self):
        def mutate(root):
            path = root / "policy-commitment.json"
            value = _read_json(path)
            value["selected_thresholds"]["duplicate"] = 0.99
            _write_json(path, value)

        self.assert_rejected("commitment|receipt", mutate)

    def test_commitment_split_mutation_is_rejected(self):
        def mutate(root):
            path = root / "policy-commitment.json"
            value = _read_json(path)
            value["split_sha256"] = "0" * 64
            _write_json(path, value)

        self.assert_rejected("split SHA", mutate)

    def test_calibration_query_set_mutation_is_rejected(self):
        def mutate(root):
            path = root / "policy-commitment.json"
            value = _read_json(path)
            value["calibration_query_ids_sha256"] = "0" * 64
            _write_json(path, value)

        self.assert_rejected("calibration-query", mutate)

    def test_invalid_preheldout_signature_is_rejected(self):
        def mutate(root):
            path = root / "pre-heldout-receipt.json"
            value = _read_json(path)
            value["signature"] = "0" * 64
            _write_json(path, value)

        self.assert_rejected("receipt signature", mutate)

    def test_missing_preheldout_signature_is_rejected(self):
        def mutate(root):
            path = root / "pre-heldout-receipt.json"
            value = _read_json(path)
            del value["signature"]
            _write_json(path, value)

        self.assert_rejected(
            "pre-held-out receipt schema is not closed", mutate
        )

    def test_expected_metric_mutation_is_rejected(self):
        def mutate(root):
            path = root / "expected-metrics.json"
            value = _read_json(path)
            value["metrics"]["retrieval-only"]["ranking"][
                "mrr_at_10"
            ] = 0.0
            _write_json(path, value)

        self.assert_rejected("expected metrics mismatch", mutate)

    def test_commitment_must_precede_receipt_and_heldout_start(self):
        def mutate(root):
            path = root / "policy-commitment.json"
            value = _read_json(path)
            value["sealed_at"] = "2026-07-23T12:30:00Z"
            _write_json(path, value)

        self.assert_rejected("sealed before.*held-out", mutate)

    def test_synthetic_capability_cannot_enable_production(self):
        bundle = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )
        with self.assertRaisesRegex(
            history_eval.BenchmarkError,
            "synthetic_contract_only.*production",
        ):
            history_eval.validate_calibration_capability(
                bundle,
                policy=history_eval.load_policy(),
                trust_root=_read_json(
                    self.benchmark / "test-witness-key.json"
                ),
                required_scope="production",
                benchmark=self.benchmark,
            )

    def test_valid_synthetic_capability_is_contract_only(self):
        bundle = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )
        value = history_eval.validate_calibration_capability(
            bundle,
            policy=history_eval.load_policy(),
            trust_root=_read_json(
                self.benchmark / "test-witness-key.json"
            ),
            required_scope="synthetic_contract_only",
            benchmark=self.benchmark,
        )
        self.assertEqual(value["scope"], "synthetic_contract_only")
        self.assertFalse(value["enforcement_eligible"])

    def test_capability_binding_and_seal_mutations_are_rejected(self):
        baseline = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )
        cases = (
            ("policy_sha256", "0" * 64),
            ("benchmark_snapshot_sha256", "0" * 64),
            ("qrels_sha256", "0" * 64),
            ("adjudications_sha256", "0" * 64),
            ("heldout_output_sha256", "0" * 64),
            ("preheldout_receipt_sha256", "0" * 64),
            ("canonical_seal_sha256", "0" * 64),
            ("signature", "0" * 64),
            ("heldout_run_nonce", 9),
            ("unresolved_adjudications", 1),
        )
        for field, value in cases:
            with self.subTest(field=field):
                bundle = copy.deepcopy(baseline)
                bundle["calibration_capability"][field] = value
                with self.assertRaises(history_eval.BenchmarkError):
                    history_eval.validate_calibration_capability(
                        bundle,
                        policy=history_eval.load_policy(),
                        trust_root=_read_json(
                            self.benchmark / "test-witness-key.json"
                        ),
                        required_scope="synthetic_contract_only",
                        benchmark=self.benchmark,
                    )

    def test_insufficient_or_advisory_relation_counts_are_rejected(self):
        baseline = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )
        for field, value in (
            ("positive", 29),
            ("hard_negative", 29),
            ("advisory", True),
        ):
            with self.subTest(field=field):
                bundle = copy.deepcopy(baseline)
                counts = bundle["calibration_capability"][
                    "relation_heldout_counts"
                ]["duplicate"]
                counts[field] = value
                with self.assertRaisesRegex(
                    history_eval.BenchmarkError,
                    "counts|advisory|seal",
                ):
                    history_eval.validate_calibration_capability(
                        bundle,
                        policy=history_eval.load_policy(),
                        trust_root=_read_json(
                            self.benchmark / "test-witness-key.json"
                        ),
                        required_scope="synthetic_contract_only",
                        benchmark=self.benchmark,
                    )


def main():
    result = unittest.main(argv=[sys.argv[0]], exit=False)
    if result.result.wasSuccessful():
        print(
            "PASS: synthetic history-retrieval benchmark "
            "matches its sealed contract"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
