#!/usr/bin/env python3
import copy
import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_budget as budget
from lib import history_cli
from lib import history_projection as projection
from lib import history_retrieval as retrieval
from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def row(story, reason="missing strong baseline", category="design-fixable"):
    return (
        "2026-07-23\thunt\tWorld Models\t"
        + story
        + "\taccept-w-rev\t"
        + reason
        + "\tlow\t"
        + category
        + "\n"
    ).encode("utf-8")


class HistoryRetrievalSmoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text(
            "retrieval-test\n", encoding="utf-8"
        )
        ledger = self.root / "ledger.tsv"
        ledger.write_bytes(
            HEADER
            + row("confidence gated world model reduces unsafe rollout")
            + row("causal world model confidence prevents unsafe rollout")
            + row(
                "latent policy audit needs stronger controls",
                "direct hit in prior work",
                "novelty-capped",
            )
        )
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        history_store.import_tsv_epoch(self.conn, ledger)
        self.policy = projection.load_policy(
            ROOT / "history/retrieval-policy-v1.json"
        )
        projection.rebuild(self.conn, self.policy)
        first = self.conn.execute(
            "SELECT * FROM candidates ORDER BY source_sequence LIMIT 1"
        ).fetchone()
        self.query = {
            "candidate_id": "query-candidate",
            "story": first["story"],
            "theme": first["theme"],
            "verdict": "accept-w-rev",
            "reason": "missing strong baseline",
            "category": "design-fixable",
        }
        self.lineage_id = first["lineage_id"]
        self.compare_role = "Classify only evidence-addressed internal relations."
        self.output_schema = {"type": "object", "required": ["status", "relations"]}
        self.tool_receipt = {"tool": "history-retrieve", "status": "complete"}
        self.complete_pack = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(self.complete_pack["retrieval_status"], "complete")
        match = self.complete_pack["lineages"][0]["matches"][0]
        self.valid_response = {
            "status": "complete_match",
            "comparator_version": "history-comparator-v1",
            "relations": [
                {
                    "relation": "same_core_idea",
                    "candidate_id": match["candidate_id"],
                    "lineage_id": match["lineage_id"],
                    "facet": match["facet"],
                    "evidence_id": match["evidence_id"],
                    "material_difference": "No material proposition change.",
                    "confidence": 0.98,
                }
            ],
            "expansion_request": None,
        }

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_every_missing_required_channel_is_partial_for_each_intent(self):
        intents = (
            "duplicate_search",
            "evolution_search",
            "failure_pattern_search",
        )
        required = ("exact", "fts", "dense", "lineage")
        for intent in intents:
            for channel in required:
                with self.subTest(intent=intent, channel=channel):
                    pack = retrieval.build_pack(
                        self.conn,
                        self.query,
                        intent,
                        self.policy,
                        disabled_channels={channel},
                    )
                    self.assertEqual(pack["retrieval_status"], "partial")
                    self.assertEqual(pack["channels"][channel]["status"], "failed")

    def test_unpublished_canonical_change_fails_all_required_channels_closed(self):
        history_store.append_rows(
            self.conn,
            [row("unpublished history candidate")],
            {"run_id": "unpublished"},
        )
        pack = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(pack["retrieval_status"], "backend_failed")
        for channel in ("exact", "fts", "dense", "lineage"):
            self.assertEqual(pack["channels"][channel]["status"], "failed")
        self.assertEqual(
            pack["channels"]["expansion"]["status"], "not_applicable"
        )

    def test_not_applicable_and_requested_expansion_contract(self):
        initial = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(
            initial["channels"]["expansion"]["status"], "not_applicable"
        )
        expanded = retrieval.build_pack(
            self.conn,
            self.query,
            "duplicate_search",
            self.policy,
            expansion_request={"lineage_ids": [self.lineage_id]},
            disabled_channels={"expansion"},
        )
        self.assertEqual(expanded["retrieval_status"], "partial")
        self.assertEqual(expanded["channels"]["expansion"]["status"], "failed")

    def test_hybrid_trace_is_deterministic_and_evidence_addressed(self):
        repeated = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(repeated, self.complete_pack)
        self.assertEqual(
            retrieval.pack_sha256(self.complete_pack),
            self.complete_pack["pack_sha256"],
        )
        self.assertEqual(
            self.complete_pack["estimated_input_tokens"],
            (len(retrieval.canonical_bytes(self.complete_pack)) + 3) // 4,
        )
        for lineage in repeated["lineages"]:
            self.assertGreater(lineage["rrf_score"], 0)
            for match in lineage["matches"]:
                self.assertEqual(
                    set(
                        (
                            "candidate_id",
                            "lineage_id",
                            "facet",
                            "raw_score",
                            "rank",
                            "source_artifact_id",
                            "source_location",
                            "evidence_id",
                            "evidence_span",
                            "material_delta",
                            "channel",
                        )
                    )
                    - set(match),
                    set(),
                )

    def test_pack_reads_one_explicit_database_snapshot(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            retrieval.build_pack(
                self.conn, self.query, "duplicate_search", self.policy
            )
        finally:
            self.conn.set_trace_callback(None)
        self.assertTrue(
            any(statement.strip().upper() == "BEGIN" for statement in statements)
        )
        self.assertFalse(self.conn.in_transaction)

    def test_failure_exact_uses_structured_failure_code(self):
        pack = retrieval.build_pack(
            self.conn,
            dict(
                self.query,
                story="new wording with no story alias",
                reason="a direct hit already exists",
                category="novelty-capped",
            ),
            "failure_pattern_search",
            self.policy,
        )
        exact = pack["channels"]["exact"]["results"]
        self.assertTrue(exact)
        self.assertTrue(all(item["facet"] == "failure_code" for item in exact))

    def test_cutoff_lineage_cannot_be_dropped_to_fit_budget(self):
        tiny = dict(self.policy, max_retrieval_tokens=1)
        pack = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", tiny
        )
        self.assertEqual(pack["retrieval_status"], "budget_exceeded")
        self.assertEqual(pack["lineages"], [])
        self.assertGreater(pack["omitted_lineage_count"], 0)

    def test_channel_payload_exposes_only_retained_lineages(self):
        history_store.append_rows(
            self.conn,
            [
                row("shared bounded retrieval candidate %02d" % index)
                for index in range(12)
            ],
            {"run_id": "bounded-channel-payload"},
        )
        projection.rebuild(self.conn, self.policy)
        pack = retrieval.build_pack(
            self.conn,
            dict(self.query, story="shared bounded retrieval candidate"),
            "duplicate_search",
            self.policy,
        )
        retained = {item["lineage_id"] for item in pack["lineages"]}
        for channel in pack["channels"].values():
            self.assertTrue(
                {item["lineage_id"] for item in channel["results"]}
                .issubset(retained)
            )
            self.assertGreaterEqual(channel["result_count"], len(channel["results"]))

    def test_comparator_preflight_serializes_pack_and_receipts(self):
        pack_bytes = retrieval.canonical_bytes(self.complete_pack)
        invocation = budget.serialize_stage_invocation(
            stage="history-compare",
            adapter_version=self.policy["adapter_version"],
            fixed_instructions=self.compare_role,
            mounted_inputs={"retrieval_pack.json": pack_bytes},
            candidate=self.query,
            retrieval_payload=self.complete_pack,
            receipts=[self.tool_receipt],
            tool_schemas=[self.output_schema],
            messages=[{"role": "user", "content": "Compare the candidate."}],
        )
        receipt = budget.preflight_stage_invocation(
            invocation,
            self.policy,
            expected_mounted_inputs={"retrieval_pack.json": pack_bytes},
        )
        self.assertIn(self.complete_pack["pack_sha256"], receipt["input_sha256s"])
        self.assertIn(hashlib.sha256(pack_bytes).hexdigest(), receipt["input_sha256s"])

    def test_noncomplete_receipt_forbids_permanent_conclusion(self):
        for status in (
            "partial",
            "backend_failed",
            "budget_exceeded",
            "uncertain",
            "conflicting_evidence",
        ):
            self.assertFalse(
                retrieval.permits_permanent_conclusion({"status": status})
            )
        for status in ("complete_match", "complete_no_match"):
            self.assertTrue(
                retrieval.permits_permanent_conclusion({"status": status})
            )

    def test_comparison_cannot_reference_evidence_outside_pack(self):
        for field, value in (
            ("candidate_id", "outside-candidate"),
            ("lineage_id", "outside-lineage"),
            ("facet", "outside-facet"),
            ("evidence_id", "outside-evidence"),
        ):
            response = copy.deepcopy(self.valid_response)
            response["relations"][0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(retrieval.ComparisonValidationError):
                    retrieval.finalize_comparison(
                        self.conn, self.complete_pack, response, self.policy
                    )

    def test_comparison_relation_must_match_retrieval_intent(self):
        response = copy.deepcopy(self.valid_response)
        response["relations"][0]["relation"] = "same_failure_mechanism"
        with self.assertRaises(retrieval.ComparisonValidationError):
            retrieval.finalize_comparison(
                self.conn, self.complete_pack, response, self.policy
            )

    def test_receipt_is_host_built_replayable_and_idempotent(self):
        receipt = retrieval.finalize_comparison(
            self.conn, self.complete_pack, self.valid_response, self.policy
        )
        repeated = retrieval.finalize_comparison(
            self.conn, self.complete_pack, self.valid_response, self.policy
        )
        self.assertEqual(repeated, receipt)
        self.assertTrue(
            retrieval.replay_receipt(
                self.conn, self.complete_pack, receipt, self.policy
            )["valid"]
        )
        stored = json.loads(
            self.conn.execute(
                "SELECT receipt_json FROM history_receipts WHERE receipt_id = ?",
                (receipt["receipt_id"],),
            ).fetchone()[0]
        )
        self.assertEqual(stored, receipt)

    def test_receipt_replays_after_a_newer_generation_is_published(self):
        receipt = retrieval.finalize_comparison(
            self.conn, self.complete_pack, self.valid_response, self.policy
        )
        history_store.append_rows(
            self.conn,
            [row("later unrelated candidate")],
            {"run_id": "later-generation"},
        )
        projection.rebuild(self.conn, self.policy)
        self.assertTrue(
            retrieval.replay_receipt(
                self.conn, self.complete_pack, receipt, self.policy
            )["valid"]
        )

    def test_receipt_replay_rejects_version_hash_watermark_and_evidence_drift(self):
        receipt = retrieval.finalize_comparison(
            self.conn, self.complete_pack, self.valid_response, self.policy
        )
        mutations = (
            ("retrieval_policy_version", "wrong-policy"),
            ("source_watermark", self.complete_pack["source_watermark"] + 1),
            ("index_generation", self.complete_pack["index_generation"] + 1),
            ("pack_sha256", "0" * 64),
            ("comparator_version", "wrong-comparator"),
        )
        for field, value in mutations:
            changed = copy.deepcopy(receipt)
            changed[field] = value
            with self.subTest(field=field):
                with self.assertRaises(retrieval.ReceiptReplayError):
                    retrieval.replay_receipt(
                        self.conn, self.complete_pack, changed, self.policy
                    )
        changed = copy.deepcopy(receipt)
        changed["relations"][0]["evidence_id"] = "outside-evidence"
        with self.assertRaises(retrieval.ReceiptReplayError):
            retrieval.replay_receipt(
                self.conn, self.complete_pack, changed, self.policy
            )

    def test_receipt_replay_recomputes_comparison_hash(self):
        receipt = retrieval.finalize_comparison(
            self.conn, self.complete_pack, self.valid_response, self.policy
        )
        changed = copy.deepcopy(receipt)
        changed["comparison_sha256"] = "0" * 64
        material = dict(changed)
        material.pop("receipt_id")
        changed["receipt_id"] = hashlib.sha256(
            b"history-receipt-v1\0" + retrieval.canonical_bytes(material)
        ).hexdigest()
        encoded = retrieval.canonical_bytes(changed).decode("utf-8").rstrip("\n")
        self.conn.execute(
            """
            INSERT INTO history_receipts(
              receipt_id, query_candidate_id, intent, pack_sha256,
              retrieval_policy_version, source_watermark, index_generation,
              comparator_version, status, receipt_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                changed["receipt_id"],
                changed["query_candidate_id"],
                changed["intent"],
                changed["pack_sha256"],
                changed["retrieval_policy_version"],
                changed["source_watermark"],
                changed["index_generation"],
                changed["comparator_version"],
                changed["status"],
                encoded,
            ),
        )
        with self.assertRaises(retrieval.ReceiptReplayError):
            retrieval.replay_receipt(
                self.conn, self.complete_pack, changed, self.policy
            )

    def test_pack_tampering_is_detected_before_comparison(self):
        changed = copy.deepcopy(self.complete_pack)
        changed["lineages"][0]["matches"][0]["evidence_span"] = "tampered"
        with self.assertRaises(retrieval.ComparisonValidationError):
            retrieval.finalize_comparison(
                self.conn, changed, self.valid_response, self.policy
            )

    def test_cli_retrieve_finalize_and_replay_round_trip(self):
        query_path = self.root / "query.json"
        pack_path = self.root / "retrieval_pack.json"
        comparison_path = self.root / "comparison.json"
        receipt_path = self.root / "history_receipt.json"
        query_path.write_text(json.dumps(self.query), encoding="utf-8")
        commands = [
            [
                "retrieve",
                "--query",
                str(query_path),
                "--policy",
                str(ROOT / "history/retrieval-policy-v1.json"),
                "--intent",
                "duplicate_search",
                "--output",
                str(pack_path),
            ],
        ]
        for command in commands:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "lib/history_cli.py"),
                    "--db",
                    str(self.root / "history.sqlite3"),
                    *command,
                ],
                cwd=str(self.root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        match = pack["lineages"][0]["matches"][0]
        comparison = copy.deepcopy(self.valid_response)
        comparison["relations"][0].update(
            {
                key: match[key]
                for key in ("candidate_id", "lineage_id", "facet", "evidence_id")
            }
        )
        comparison_path.write_text(json.dumps(comparison), encoding="utf-8")
        for command in (
            [
                "finalize-comparison",
                "--pack",
                str(pack_path),
                "--policy",
                str(ROOT / "history/retrieval-policy-v1.json"),
                "--comparison",
                str(comparison_path),
                "--output",
                str(receipt_path),
            ],
            [
                "replay-receipt",
                "--pack",
                str(pack_path),
                "--policy",
                str(ROOT / "history/retrieval-policy-v1.json"),
                "--receipt",
                str(receipt_path),
            ],
        ):
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "lib/history_cli.py"),
                    "--db",
                    str(self.root / "history.sqlite3"),
                    *command,
                ],
                cwd=str(self.root),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_json_artifacts_cannot_replace_canonical_ledger(self):
        ledger = self.root / "ledger.tsv"
        preserved = ledger.read_bytes()
        with self.assertRaises(ValueError):
            history_cli.write_json_artifact(
                self.conn, ledger, self.complete_pack
            )
        self.assertEqual(ledger.read_bytes(), preserved)


if __name__ == "__main__":
    unittest.main()
