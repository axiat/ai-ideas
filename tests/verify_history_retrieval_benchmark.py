#!/usr/bin/env python3
"""Offline verifier for the history-retrieval benchmark contract."""

import copy
import json
import pathlib
import shutil
import subprocess
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

    def reseal_capability(self, capability, trust_root=None):
        capability.pop("canonical_seal_sha256", None)
        capability.pop("signature", None)
        capability["canonical_seal_sha256"] = history_eval.sha256(
            b"history-calibration-capability-v1\0"
            + history_eval.canonical_bytes(
                history_eval._capability_seal_material(capability)
            )
        )
        if trust_root is None:
            capability["signature"] = "1" * 64
        else:
            capability["signature"] = history_eval._test_signature(
                b"history-calibration-capability-signature-v1\0",
                capability,
                trust_root,
            )

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

    def test_history_cli_evaluate_needs_no_database(self):
        with tempfile.TemporaryDirectory(
            prefix="history-eval-cli."
        ) as temporary:
            output = pathlib.Path(temporary) / "evaluation.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "lib/history_cli.py"),
                    "evaluate",
                    "--benchmark",
                    str(self.benchmark),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr
            )
            summary = json.loads(completed.stdout)
            self.assertEqual(
                summary["scope"], "synthetic_contract_only"
            )
            artifact = _read_json(output)
            self.assertEqual(
                artifact["scope"], "synthetic_contract_only"
            )
            self.assertIn(
                "confidence_intervals", artifact
            )

    def test_ranking_metrics_condense_to_judged_pairs(self):
        result = history_eval.evaluate_benchmark(
            self.benchmark, verify_expected=False
        )
        ranking = result["metrics"]["retrieval-only"]["ranking"]
        self.assertEqual(ranking["mrr_at_10"], 1.0)
        self.assertEqual(ranking["hit_at"]["1"], 1.0)
        self.assertEqual(ranking["judged_ranked_pairs"], 9)
        self.assertEqual(ranking["unjudged_ranked_pairs"], 9)
        self.assertEqual(ranking["incomplete_judgment_queries"], 1)

    def test_required_metrics_and_confidence_intervals_have_literal_values(
        self,
    ):
        result = history_eval.evaluate_benchmark(
            self.benchmark, verify_expected=False
        )
        retrieval = result["metrics"]["retrieval-only"][
            "required_metrics"
        ]
        self.assertEqual(
            retrieval["duplicate"]["hit_at"],
            {"1": 1.0, "3": 1.0, "5": 1.0, "10": 1.0},
        )
        self.assertEqual(
            retrieval["duplicate"]["mrr_at_10"], 1.0
        )
        self.assertEqual(
            retrieval["duplicate"]["alert_precision"], 0.5
        )
        self.assertEqual(
            retrieval["duplicate"][
                "no_hit_false_positive_rate"
            ],
            1.0,
        )
        self.assertEqual(
            retrieval["lineage"]["direct_parent_accuracy_at_1"],
            0.0,
        )
        self.assertEqual(
            retrieval["lineage"]["ancestor_recall_at"],
            {"5": 1.0, "10": 1.0},
        )
        self.assertEqual(
            retrieval["failure"]["recall_at"]["1"], 0.5
        )
        intervals = result["confidence_intervals"]
        self.assertEqual(intervals["seed"], 20260723)
        self.assertEqual(intervals["samples"], 2000)
        self.assertIn("retrieval-only", intervals["arms"])
        self.assertIn(
            "end-to-end_minus_closed-book",
            intervals["system_differences"],
        )
        duplicate_ci = intervals["arms"]["retrieval-only"][
            "duplicate_hit_at_10"
        ]
        self.assertEqual(duplicate_ci["estimate"], 1.0)
        self.assertEqual(
            duplicate_ci["ci95"], {"lower": 1.0, "upper": 1.0}
        )

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

    def test_future_revision_text_cannot_leak_into_query_text(self):
        corpus_rows = [
            {
                "schema_version": 1,
                "record_id": "r-visible",
                "lineage_id": "lin-visible",
                "committed_at": "2026-01-01T00:00:00Z",
                "text": "Visible historical proposal.",
                "verdict": "accept",
                "reason": "Visible reason.",
                "citations": ["visible-citation"],
            },
            {
                "schema_version": 1,
                "record_id": "r-future-revision",
                "lineage_id": "lin-visible",
                "committed_at": "2026-03-01T00:00:00Z",
                "text": "Future revision changes the causal estimand.",
                "verdict": "pending",
                "reason": "Future reason.",
                "citations": ["future-citation"],
            },
        ]
        corpus = history_eval._validate_corpus(corpus_rows)
        query = {
            "schema_version": 1,
            "query_id": "q-temporal",
            "relation_set": "duplicate",
            "lineage_id": "lin-visible",
            "fold": "test",
            "as_of": "2026-02-01T00:00:00Z",
            "corpus_watermark": "2026-01-01T00:00:00Z",
            "text": "Future revision changes the causal estimand.",
            "expected_abstain": False,
            "theme": "Safety and Robustness",
            "lexical_overlap_bucket": "high",
            "history_age_bucket": "recent",
        }
        with self.assertRaisesRegex(
            history_eval.BenchmarkError, "future revision"
        ):
            history_eval._validate_queries([query], corpus)

    def test_temporal_watermark_uses_parsed_utc_order(self):
        corpus_rows = [
            {
                "schema_version": 1,
                "record_id": "r-whole-second",
                "lineage_id": "lin-time",
                "committed_at": "2026-01-01T00:00:00Z",
                "text": "Whole-second record.",
                "verdict": "accept",
                "reason": "Whole-second reason.",
                "citations": ["whole-second-citation"],
            },
            {
                "schema_version": 1,
                "record_id": "r-fractional-second",
                "lineage_id": "lin-time",
                "committed_at": "2026-01-01T00:00:00.900000Z",
                "text": "Fractional-second record.",
                "verdict": "accept",
                "reason": "Fractional-second reason.",
                "citations": ["fractional-second-citation"],
            },
        ]
        corpus = history_eval._validate_corpus(corpus_rows)
        query = {
            "schema_version": 1,
            "query_id": "q-watermark",
            "relation_set": "duplicate",
            "lineage_id": "lin-time",
            "fold": "test",
            "as_of": "2026-01-01T00:00:01Z",
            "corpus_watermark":
                "2026-01-01T00:00:00.900000Z",
            "text": "Independent temporal query.",
            "expected_abstain": False,
            "theme": "Safety and Robustness",
            "lexical_overlap_bucket": "low",
            "history_age_bucket": "recent",
        }
        validated = history_eval._validate_queries([query], corpus)
        self.assertEqual(set(validated), {"q-watermark"})

    def test_benchmark_child_symlink_is_rejected(self):
        benchmark = self.copied_benchmark()
        path = benchmark / "queries.jsonl"
        path.unlink()
        path.symlink_to(self.benchmark / "queries.jsonl")
        with self.assertRaisesRegex(
            history_eval.BenchmarkError, "symlink"
        ):
            history_eval.evaluate_benchmark(benchmark)

    def test_policy_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(
            prefix="history-policy."
        ) as temporary:
            path = pathlib.Path(temporary) / "policy.json"
            path.symlink_to(history_eval.DEFAULT_POLICY)
            with self.assertRaisesRegex(
                history_eval.BenchmarkError, "symlink"
            ):
                history_eval.evaluate_benchmark(
                    self.benchmark, policy_path=path
                )

    def test_policy_requires_bounded_canonical_json(self):
        with tempfile.TemporaryDirectory(
            prefix="history-policy."
        ) as temporary:
            path = pathlib.Path(temporary) / "policy.json"
            path.write_text(
                json.dumps(
                    history_eval.load_policy(), indent=2
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                history_eval.BenchmarkError, "canonical JSON"
            ):
                history_eval.evaluate_benchmark(
                    self.benchmark, policy_path=path
                )

    def test_retrieval_only_unused_fields_must_be_empty(self):
        def mutate(root):
            path = root / "outputs/retrieval-only.jsonl"
            rows = _read_jsonl(path)
            rows[0]["relation"] = "blocking"
            _write_jsonl(path, rows)

        self.assert_rejected(
            "retrieval-only.*unused fields", mutate
        )

    def test_no_match_status_and_relation_are_biconditional(self):
        def mutate(root):
            path = root / "outputs/end-to-end.jsonl"
            rows = _read_jsonl(path)
            row = next(
                item
                for item in rows
                if item["query_id"] == "q-dup-hit"
            )
            row["relation"] = "blocking"
            row["status"] = "complete_no_match"
            _write_jsonl(path, rows)

        self.assert_rejected(
            "complete_no_match.*no_match", mutate
        )

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
        self.assertFalse(value["evaluation_evidence"][
            "all_gates_passed"
        ])

    def test_synthetic_capability_uses_derived_per_label_coverage(self):
        capability = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )["calibration_capability"]
        counts = capability["relation_heldout_counts"]
        self.assertEqual(
            set(counts),
            {
                "blocking",
                "substantive",
                "direct-parent",
                "ancestor-or-descendant",
                "sibling",
                "same-mechanism",
                "related-defect",
            },
        )
        self.assertEqual(
            counts["blocking"],
            {"positive": 1, "hard_negative": 2, "advisory": True},
        )
        self.assertEqual(
            counts["substantive"],
            {"positive": 0, "hard_negative": 2, "advisory": True},
        )
        self.assertTrue(
            all(value["advisory"] for value in counts.values())
        )
        evidence = capability["evaluation_evidence"]
        self.assertEqual(
            evidence["error_budgets"][
                "max_false_duplicate_rate"
            ]["observed"],
            0.5,
        )
        self.assertEqual(
            evidence["error_budgets"][
                "max_false_internal_no_match_rate"
            ]["observed"],
            0.333333333333,
        )
        self.assertFalse(evidence["all_gates_passed"])

    def test_resealed_fabricated_coverage_is_rejected(self):
        bundle = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )
        trust_root = _read_json(
            self.benchmark / "test-witness-key.json"
        )
        counts = bundle["calibration_capability"][
            "relation_heldout_counts"
        ]["blocking"]
        counts.update(
            {"positive": 30, "hard_negative": 30, "advisory": False}
        )
        capability = bundle["calibration_capability"]
        self.reseal_capability(capability, trust_root)
        with self.assertRaisesRegex(
            history_eval.BenchmarkError, "derived held-out coverage"
        ):
            history_eval.validate_calibration_capability(
                bundle,
                policy=history_eval.load_policy(),
                trust_root=trust_root,
                required_scope="synthetic_contract_only",
                benchmark=self.benchmark,
            )

    def test_resealed_fabricated_evaluation_evidence_is_rejected(self):
        bundle = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )
        trust_root = _read_json(
            self.benchmark / "test-witness-key.json"
        )
        capability = bundle["calibration_capability"]
        gate = capability["evaluation_evidence"][
            "error_budgets"
        ]["max_false_duplicate_rate"]
        gate.update(
            {
                "observed": 0.0,
                "ci95_upper": 0.0,
                "passed": True,
            }
        )
        self.reseal_capability(capability, trust_root)
        with self.assertRaisesRegex(
            history_eval.BenchmarkError,
            "derived evaluation evidence",
        ):
            history_eval.validate_calibration_capability(
                bundle,
                policy=history_eval.load_policy(),
                trust_root=trust_root,
                required_scope="synthetic_contract_only",
                benchmark=self.benchmark,
            )

    def test_capability_artifact_scope_is_fail_closed(self):
        capability = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )["calibration_capability"]
        with self.assertRaisesRegex(
            history_eval.BenchmarkError,
            "synthetic_contract_only.*production",
        ):
            history_eval.validate_capability_artifact(
                capability, required_scope="production"
            )

    def test_production_receipt_rejects_synthetic_test_root(self):
        commitment = _read_json(
            self.benchmark / "policy-commitment.json"
        )
        commitment["scope"] = "production"
        receipt = _read_json(
            self.benchmark / "pre-heldout-receipt.json"
        )
        receipt["scope"] = "production"
        receipt["policy_commitment_sha256"] = history_eval.sha256(
            history_eval.canonical_bytes(commitment)
        )
        receipt["signature"] = "1" * 64
        trust_root = _read_json(
            self.benchmark / "test-witness-key.json"
        )
        with self.assertRaisesRegex(
            history_eval.BenchmarkError,
            "synthetic.*production",
        ):
            history_eval._validate_receipt(
                receipt,
                commitment,
                trust_root,
                "production",
                "2026-07-23T12:00:00Z",
                witness_verifier=lambda *_: True,
            )

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

    def test_relation_count_semantics_are_fail_closed(self):
        baseline = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )
        for field, value in (
            ("positive", -1),
            ("hard_negative", -1),
            ("advisory", False),
        ):
            with self.subTest(field=field):
                bundle = copy.deepcopy(baseline)
                counts = bundle["calibration_capability"][
                    "relation_heldout_counts"
                ]["blocking"]
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

    def test_production_requires_every_positive_label_non_advisory(self):
        capability = history_eval.build_synthetic_capability_for_test(
            self.benchmark
        )["calibration_capability"]
        capability["scope"] = "production"
        capability["trust_root_id"] = "production-root"
        for counts in capability[
            "relation_heldout_counts"
        ].values():
            counts.update(
                {
                    "positive": 30,
                    "hard_negative": 30,
                    "advisory": False,
                }
            )
        capability["relation_heldout_counts"]["substantive"].update(
            {"positive": 29, "advisory": True}
        )
        capability["evaluation_evidence"][
            "all_gates_passed"
        ] = False
        self.reseal_capability(capability)
        with self.assertRaisesRegex(
            history_eval.BenchmarkError,
            "advisory|failed",
        ):
            history_eval.validate_capability_artifact(
                capability, required_scope="production"
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
