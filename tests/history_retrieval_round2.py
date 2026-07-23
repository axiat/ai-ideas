#!/usr/bin/env python3
"""Round-two adversarial contracts for bounded history retrieval."""

import copy
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_projection as projection
from lib import history_retrieval as retrieval
from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def ledger_row(story, reason="missing strong baseline", category="design-fixable"):
    return (
        "2026-07-23\thunt\tWorld Models\t"
        + story
        + "\taccept-w-rev\t"
        + reason
        + "\tlow\t"
        + category
        + "\n"
    ).encode("utf-8")


class RoundTwoFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.ledger = self.root / "ledger.tsv"
        (self.root / "ledger.instance-id").write_text(
            "round-two\n", encoding="utf-8"
        )
        self.ledger.write_bytes(
            HEADER + ledger_row("confidence world model unsafe rollout")
        )
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        history_store.import_tsv_epoch(self.conn, self.ledger)
        self.policy = projection.load_policy(
            ROOT / "history/retrieval-policy-v1.json"
        )
        projection.rebuild(self.conn, self.policy)
        candidate = self.conn.execute(
            "SELECT * FROM candidates ORDER BY source_sequence LIMIT 1"
        ).fetchone()
        self.candidate_id = candidate["candidate_id"]
        self.query = {
            "candidate_id": "round-two-query",
            "story": candidate["story"],
            "theme": candidate["theme"],
            "reason": candidate["reason"],
            "category": candidate["category"],
            "verdict": candidate["verdict"],
        }
        self.pack = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        match = self.pack["lineages"][0]["matches"][0]
        self.match_response = {
            "status": "complete_match",
            "comparator_version": retrieval.COMPARATOR_VERSION,
            "relations": [{
                "relation": "same_core_idea",
                "candidate_id": match["candidate_id"],
                "lineage_id": match["lineage_id"],
                "facet": match["facet"],
                "evidence_id": match["evidence_id"],
                "material_difference": "",
                "confidence": 1.0,
            }],
            "expansion_request": None,
        }

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _verify(self, response):
        receipt = retrieval.finalize_comparison(
            self.conn, self.pack, response, self.policy
        )
        return receipt, retrieval.replay_receipt(
            self.conn, self.pack, receipt, self.policy
        )

    def _install_edge_artifact(self, artifact_id):
        self.conn.execute(
            """
            INSERT INTO artifacts(
              artifact_id, kind, state, sha256, byte_count, source_path,
              source_sequence, provenance_json, idempotency_key
            ) VALUES(?, 'lineage-evidence', 'installed', ?, 1, ?, 1, '{}', ?)
            """,
            (
                artifact_id,
                hashlib.sha256(artifact_id.encode("utf-8")).hexdigest(),
                "evidence/" + artifact_id,
                artifact_id,
            ),
        )


class VerifiedCapabilityTests(RoundTwoFixture):
    def test_verified_receipt_is_immutable_and_gate_uses_sealed_decision(self):
        permanent_receipt, permanent = self._verify(self.match_response)
        uncertain_response = copy.deepcopy(self.match_response)
        uncertain_response["status"] = "uncertain"
        uncertain_response["relations"][0]["relation"] = "uncertain"
        uncertain_receipt, uncertain = self._verify(uncertain_response)

        self.assertTrue(retrieval.permits_permanent_conclusion(permanent))
        self.assertFalse(retrieval.permits_permanent_conclusion(uncertain))
        for capability, receipt in (
            (permanent, permanent_receipt),
            (uncertain, uncertain_receipt),
        ):
            with self.subTest(status=receipt["status"], mutation="assignment"):
                with self.assertRaises((AttributeError, TypeError)):
                    capability["status"] = "complete_match"
            with self.subTest(status=receipt["status"], mutation="update"):
                with self.assertRaises((AttributeError, TypeError)):
                    capability.update({
                        "status": "complete_match",
                        "receipt_id": "forged",
                    })
            with self.subTest(status=receipt["status"], mutation="delete"):
                with self.assertRaises((AttributeError, TypeError)):
                    del capability["status"]
            with self.subTest(status=receipt["status"], mutation="attribute"):
                with self.assertRaises((AttributeError, TypeError)):
                    capability.status = "complete_match"

        self.assertEqual(permanent["receipt_id"], permanent_receipt["receipt_id"])
        self.assertEqual(uncertain["receipt_id"], uncertain_receipt["receipt_id"])
        self.assertTrue(retrieval.permits_permanent_conclusion(permanent))
        self.assertFalse(retrieval.permits_permanent_conclusion(uncertain))


class ProjectionSnapshotTests(RoundTwoFixture):
    def test_pending_facet_update_is_partial_then_published_once(self):
        prior_revision = self.conn.execute(
            """
            SELECT canonical_revision
            FROM search_index_generations
            WHERE generation = ?
            """,
            (self.pack["index_generation"],),
        ).fetchone()[0]
        projection.update_candidate_facets(
            self.conn,
            self.candidate_id,
            {"mechanism": "newly queued mechanism"},
        )
        canonical_revision = int(
            self.conn.execute(
                """
                SELECT value FROM schema_meta
                WHERE key = 'history_search_content_revision'
                """
            ).fetchone()[0]
        )
        queued_revision = self.conn.execute(
            """
            SELECT max(canonical_revision)
            FROM search_projection_outbox
            WHERE state = 'pending'
            """
        ).fetchone()[0]
        self.assertGreater(canonical_revision, prior_revision)
        self.assertEqual(queued_revision, canonical_revision)
        pending = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(pending["retrieval_status"], "partial")
        self.assertEqual(
            pending["index_generation"], self.pack["index_generation"]
        )

        rebuilt = projection.rebuild(self.conn, self.policy)
        published = retrieval.build_pack(
            self.conn,
            dict(
                self.query,
                story="newly queued mechanism",
                facets={"mechanism": "newly queued mechanism"},
            ),
            "evolution_search",
            self.policy,
        )
        self.assertEqual(published["retrieval_status"], "complete")
        self.assertEqual(
            published["index_generation"], rebuilt["index_generation"]
        )
        self.assertGreater(
            published["index_generation"], pending["index_generation"]
        )
        generation = self.conn.execute(
            """
            SELECT canonical_revision, manifest_json
            FROM search_index_generations
            WHERE generation = ?
            """,
            (published["index_generation"],),
        ).fetchone()
        self.assertEqual(generation["canonical_revision"], canonical_revision)
        self.assertEqual(
            json.loads(generation["manifest_json"])["canonical_revision"],
            canonical_revision,
        )
        self.assertTrue(
            any(
                match["facet"] == "mechanism"
                and "newly queued mechanism" in match["evidence_span"]
                for lineage in published["lineages"]
                for match in lineage["matches"]
            )
        )

    def test_pending_exclusion_and_supersession_fail_closed_until_publish(self):
        projection.remove_candidate_from_search(self.conn, self.candidate_id)
        excluded_pending = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(excluded_pending["retrieval_status"], "partial")
        projection.rebuild(self.conn, self.policy)
        excluded = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(excluded["retrieval_status"], "complete")
        self.assertFalse(
            any(
                match["candidate_id"] == self.candidate_id
                for lineage in excluded["lineages"]
                for match in lineage["matches"]
            )
        )

        appended = history_store.append_rows(
            self.conn,
            [ledger_row("confidence world model unsafe rollout")],
            {"run_id": "round-two-supersession"},
        )
        child_id = appended["candidate_ids"][0]
        projection.rebuild(self.conn, self.policy)
        self._install_edge_artifact("round-two-supersession-edge")
        history_store.add_lineage_edge(
            self.conn,
            self.candidate_id,
            child_id,
            "supersedes",
            "round-two-supersession-edge",
            "explicit",
        )
        edge_queue = self.conn.execute(
            """
            SELECT record_id, canonical_revision
            FROM search_projection_outbox
            WHERE state = 'pending'
              AND content_version LIKE 'lineage-edge-v1:%'
            ORDER BY record_id
            """
        ).fetchall()
        self.assertEqual(
            {row["record_id"] for row in edge_queue},
            {self.candidate_id, child_id},
        )
        self.assertEqual(
            len({row["canonical_revision"] for row in edge_queue}), 1
        )
        supersession_pending = retrieval.build_pack(
            self.conn, self.query, "evolution_search", self.policy
        )
        self.assertEqual(supersession_pending["retrieval_status"], "partial")
        projection.rebuild(self.conn, self.policy)
        supersession = retrieval.build_pack(
            self.conn, self.query, "evolution_search", self.policy
        )
        self.assertEqual(supersession["retrieval_status"], "complete")
        self.assertTrue(
            any(
                match.get("relation_type") == "supersedes"
                for lineage in supersession["lineages"]
                for match in lineage["matches"]
            )
        )

    def test_dense_evidence_uses_published_text_not_live_facets(self):
        baseline = retrieval.build_pack(
            self.conn,
            dict(self.query, story="unrelated dense probe"),
            "duplicate_search",
            self.policy,
        )
        baseline_dense = [
            match
            for lineage in baseline["lineages"]
            for match in lineage["matches"]
            if match["channel"] == "dense"
        ]
        self.assertTrue(baseline_dense)
        self.conn.execute(
            """
            UPDATE candidate_facets
            SET text = 'MUTATED-LIVE-FACET', content_hash = ?
            WHERE candidate_id = ? AND facet = 'problem_estimand'
            """,
            (
                projection._content_hash("MUTATED-LIVE-FACET"),
                self.candidate_id,
            ),
        )
        repeated = retrieval.build_pack(
            self.conn,
            dict(self.query, story="unrelated dense probe"),
            "duplicate_search",
            self.policy,
        )
        repeated_dense = [
            match
            for lineage in repeated["lineages"]
            for match in lineage["matches"]
            if match["channel"] == "dense"
        ]
        self.assertEqual(
            [
                (item["candidate_id"], item["facet"], item["evidence_span"])
                for item in repeated_dense
            ],
            [
                (item["candidate_id"], item["facet"], item["evidence_span"])
                for item in baseline_dense
            ],
        )


class LineageUnitTests(RoundTwoFixture):
    @staticmethod
    def _lineage(lineage_number):
        lineage_id = "lineage-%d" % lineage_number
        return {
            "lineage_id": lineage_id,
            "rank": lineage_number,
            "rrf_score": 1.0 / lineage_number,
            "matches": [
                {
                    "candidate_id": lineage_id + "-parent",
                    "lineage_id": lineage_id,
                    "channel": "lineage",
                    "facet": "lineage",
                    "rank": 1,
                    "version_role": "highest_match",
                },
                {
                    "candidate_id": lineage_id + "-child",
                    "lineage_id": lineage_id,
                    "channel": "lineage",
                    "facet": "lineage",
                    "rank": 2,
                    "version_role": "current",
                },
            ],
        }

    def test_evolution_cap_keeps_complete_units_or_fails_closed(self):
        retained = retrieval._cap_matches(
            [self._lineage(index) for index in range(1, 6)],
            10,
            intent="evolution_search",
        )
        self.assertEqual(len(retained), 5)
        for lineage in retained:
            self.assertEqual(
                {match["version_role"] for match in lineage["matches"]},
                {"highest_match", "current"},
            )
        with self.assertRaises(retrieval.RetrievalError):
            retrieval._cap_matches(
                [self._lineage(index) for index in range(1, 7)],
                10,
                intent="evolution_search",
            )

    def test_lineage_evidence_binds_exact_endpoints_and_direction(self):
        appended = history_store.append_rows(
            self.conn,
            [ledger_row("confidence world model unsafe rollout")],
            {"run_id": "round-two-edge"},
        )
        child_id = appended["candidate_ids"][0]
        self._install_edge_artifact("round-two-edge")
        history_store.add_lineage_edge(
            self.conn,
            self.candidate_id,
            child_id,
            "evolved_from",
            "round-two-edge",
            "explicit",
        )
        projection.rebuild(self.conn, self.policy)
        pack = retrieval.build_pack(
            self.conn, self.query, "evolution_search", self.policy
        )
        lineage_matches = [
            match
            for lineage in pack["lineages"]
            for match in lineage["matches"]
            if match["channel"] == "lineage"
        ]
        self.assertTrue(lineage_matches)
        self.assertEqual(
            {match["parent_candidate_id"] for match in lineage_matches},
            {self.candidate_id},
        )
        self.assertEqual(
            {match["child_candidate_id"] for match in lineage_matches},
            {child_id},
        )
        self.assertTrue(
            all(
                match["candidate_id"]
                == match[match["edge_direction"] + "_candidate_id"]
                for match in lineage_matches
            )
        )


class PublicationAuditTests(RoundTwoFixture):
    def test_publication_preflight_binds_canonical_invocation(self):
        publication = self.conn.execute(
            """
            SELECT * FROM history_pack_publications
            WHERE publication_id = ?
            """,
            (self.pack["pack_publication_id"],),
        ).fetchone()
        invocation_bytes = retrieval.comparator_invocation_bytes(
            self.pack, self.policy
        )
        invocation = json.loads(invocation_bytes)
        self.assertEqual(
            invocation_bytes, retrieval.canonical_bytes(invocation)
        )
        preflight_bytes = publication["comparator_preflight_json"].encode(
            "utf-8"
        )
        preflight = json.loads(preflight_bytes)
        self.assertEqual(
            preflight["serialized_sha256"],
            hashlib.sha256(invocation_bytes).hexdigest(),
        )
        self.assertEqual(
            publication["comparator_preflight_sha256"],
            hashlib.sha256(preflight_bytes).hexdigest(),
        )

        receipt = retrieval.finalize_comparison(
            self.conn, self.pack, self.match_response, self.policy
        )
        self.assertEqual(
            receipt["comparator_preflight_sha256"],
            publication["comparator_preflight_sha256"],
        )
        verified = retrieval.replay_receipt(
            self.conn, self.pack, receipt, self.policy
        )
        self.assertTrue(verified["verified"])


class ActualLedgerBudgetTests(unittest.TestCase):
    def test_committed_ledger_budget_calibration_is_characterized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            ledger = root / "ledger.tsv"
            ledger.write_bytes((ROOT / "ledger.tsv").read_bytes())
            (root / "ledger.instance-id").write_text(
                "round-two-real-ledger\n", encoding="utf-8"
            )
            conn = history_store.connect(root / "history.sqlite3")
            try:
                history_store.init_schema(conn)
                history_store.import_tsv_epoch(conn, ledger)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
                    531,
                )
                policy = projection.load_policy(
                    ROOT / "history/retrieval-policy-v1.json"
                )
                projection.rebuild(conn, policy)
                first = conn.execute(
                    "SELECT * FROM candidates ORDER BY source_sequence LIMIT 1"
                ).fetchone()
                queries = {
                    "exact": (
                        {
                            "candidate_id": "actual-exact",
                            "story": first["story"],
                            "theme": first["theme"],
                        },
                        "duplicate_search",
                    ),
                    "no_hit": (
                        {
                            "candidate_id": "actual-no-hit",
                            "story": "xylophonic quasar zephyr 9d6f42",
                            "theme": "unseen theme 9d6f42",
                        },
                        "duplicate_search",
                    ),
                    "evolution": (
                        {
                            "candidate_id": "actual-evolution",
                            "story": first["story"],
                            "theme": first["theme"],
                        },
                        "evolution_search",
                    ),
                    "failure": (
                        {
                            "candidate_id": "actual-failure",
                            "story": "unrelated failure wording",
                            "theme": first["theme"],
                            "verdict": first["verdict"],
                            "reason": first["reason"],
                            "category": first["category"],
                        },
                        "failure_pattern_search",
                    ),
                }
                for label, (query, intent) in queries.items():
                    with self.subTest(label=label):
                        pack = retrieval.build_pack(
                            conn, query, intent, policy
                        )
                        self.assertEqual(
                            pack["retrieval_status"], "budget_exceeded"
                        )
                        self.assertLessEqual(
                            len(retrieval.canonical_bytes(pack))
                            + policy["adapter_wrapper_allowance"],
                            policy["max_retrieval_tokens"],
                        )
                        self.assertFalse(pack["lineages"])
                        publication = conn.execute(
                            """
                            SELECT rank_trace_json
                            FROM history_pack_publications
                            WHERE publication_id = ?
                            """,
                            (pack["pack_publication_id"],),
                        ).fetchone()
                        trace = json.loads(publication[0])
                        self.assertTrue(trace["fusion"]["lineage_order"])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
