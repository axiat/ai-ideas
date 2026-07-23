#!/usr/bin/env python3
import copy
import hashlib
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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


class HistoryRetrievalAdversarial(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text("adversarial\n", encoding="utf-8")
        self.ledger = self.root / "ledger.tsv"
        self.ledger.write_bytes(
            HEADER
            + row("confidence world model unsafe rollout")
        )
        self.db = self.root / "history.sqlite3"
        self.conn = history_store.connect(self.db)
        history_store.init_schema(self.conn)
        history_store.import_tsv_epoch(self.conn, self.ledger)
        self.policy = projection.load_policy(ROOT / "history/retrieval-policy-v1.json")
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
        self.pack = retrieval.build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(self.pack["retrieval_status"], "complete")
        match = self.pack["lineages"][0]["matches"][0]
        self.response = {
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

    def _rehash_pack(self, pack):
        pack["pack_sha256"] = retrieval.pack_sha256(pack)
        pack["receipt_id"] = hashlib.sha256(
            b"retrieval-pack-v1\0" + pack["pack_sha256"].encode("ascii")
        ).hexdigest()

    def _rehash_receipt(self, receipt):
        material = dict(receipt)
        material.pop("receipt_id", None)
        receipt["receipt_id"] = hashlib.sha256(
            b"history-receipt-v1\0" + retrieval.canonical_bytes(material)
        ).hexdigest()

    def test_self_hashed_unpublished_pack_cannot_finalize(self):
        fabricated = copy.deepcopy(self.pack)
        fabricated["query"]["candidate_id"] = "fabricated-query"
        self._rehash_pack(fabricated)
        with self.assertRaises(retrieval.ComparisonValidationError):
            retrieval.finalize_comparison(
                self.conn, fabricated, self.response, self.policy
            )

    def test_pack_publication_binds_exact_bytes_policy_and_generation_manifest(self):
        publication = self.conn.execute(
            "SELECT * FROM history_pack_publications WHERE publication_id = ?",
            (self.pack["pack_publication_id"],),
        ).fetchone()
        self.assertIsNotNone(publication)
        self.assertEqual(bytes(publication["pack_bytes"]), retrieval.canonical_bytes(self.pack))
        self.assertEqual(publication["policy_sha256"], projection._policy_sha256(self.policy))
        generation = self.conn.execute(
            "SELECT * FROM history_generation_provenance WHERE generation = ?",
            (self.pack["index_generation"],),
        ).fetchone()
        self.assertEqual(
            publication["generation_manifest_sha256"],
            generation["manifest_sha256"],
        )

    def test_same_version_policy_mutation_is_rejected(self):
        for field, value in (
            ("max_retrieval_tokens", self.policy["max_retrieval_tokens"] - 1),
            ("rrf_k", self.policy["rrf_k"] + 1),
            ("max_matches", self.policy["max_matches"] + 1),
        ):
            with self.subTest(field=field):
                with self.assertRaises(retrieval.RetrievalError):
                    retrieval.build_pack(
                        self.conn,
                        self.query,
                        "duplicate_search",
                        dict(self.policy, **{field: value}),
                    )

    def test_every_json_writer_rejects_database_sidecars_state_and_ledgers(self):
        brief = projection.build_generation_brief(self.conn, self.policy)
        targets = [
            self.db,
            pathlib.Path(str(self.db) + "-wal"),
            pathlib.Path(str(self.db) + "-shm"),
            pathlib.Path(str(self.db) + "-journal"),
            self.root / ".ai-ideas",
            self.ledger,
            self.root / "tmp" / "ledger.good",
        ]
        (self.root / "tmp").mkdir(exist_ok=True)
        for target in targets:
            with self.subTest(target=target):
                for writer, value in (
                    (history_cli.write_generation_brief, brief),
                    (history_cli.write_json_artifact, self.pack),
                ):
                    with self.assertRaises(ValueError):
                        writer(self.conn, target, value)

    def test_failure_channels_use_published_failure_fts_and_vectors(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            pack = retrieval.build_pack(
                self.conn,
                dict(
                    self.query,
                    story="unrelated wording",
                    reason="missing strong baseline",
                    category="design-fixable",
                ),
                "failure_pattern_search",
                self.policy,
            )
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(pack["retrieval_status"], "complete")
        self.assertTrue(any("MATCH" in statement.upper() for statement in statements))
        self.assertTrue(
            self.conn.execute(
                "SELECT 1 FROM search_vectors WHERE facet = 'failure_pattern' LIMIT 1"
            ).fetchone()
        )
        manifest = json.loads(
            self.conn.execute(
                "SELECT manifest_json FROM search_index_generations "
                "ORDER BY generation DESC LIMIT 1"
            ).fetchone()[0]
        )
        self.assertIn("failure_pattern", {item["facet"] for item in manifest["vectors"]})

    def test_facet_ranks_remain_independent_through_rrf(self):
        candidate_id = self.pack["lineages"][0]["matches"][0]["candidate_id"]
        contribution = next(
            item for item in self.pack["rank_contributions"]
            if item["candidate_id"] == candidate_id
        )
        pairs = {(item["channel"], item["facet"]) for item in contribution["ranks"]}
        self.assertIn(("fts", "problem_estimand"), pairs)
        self.assertIn(("fts", "claimed_delta"), pairs)
        self.assertIn(("dense", "problem_estimand"), pairs)
        self.assertIn(("dense", "claimed_delta"), pairs)

    def test_rrf_counts_one_rank_per_channel_facet_candidate(self):
        match = copy.deepcopy(self.pack["lineages"][0]["matches"][0])
        first = dict(match, channel="fts", facet="problem_estimand", rank=1)
        repeated = dict(first, rank=2, evidence_id="later-evidence")
        ranked, contributions = retrieval._fuse(
            {"fts": [repeated, first]}, self.policy
        )
        self.assertEqual(len(contributions), 1)
        self.assertEqual(len(contributions[0]["ranks"]), 1)
        self.assertEqual(contributions[0]["ranks"][0]["rank"], 1)
        self.assertAlmostEqual(
            ranked[0]["rrf_score"],
            1.0 / (self.policy["rrf_k"] + 1),
            places=12,
        )

    def test_full_rank_trace_is_host_owned_while_pack_trace_is_bounded(self):
        publication = self.conn.execute(
            """
            SELECT rank_trace_json, rank_trace_sha256,
                   comparator_preflight_json, comparator_preflight_sha256
            FROM history_pack_publications
            WHERE publication_id = ?
            """,
            (self.pack["pack_publication_id"],),
        ).fetchone()
        trace_bytes = publication["rank_trace_json"].encode("utf-8")
        trace = json.loads(trace_bytes)
        self.assertEqual(trace_bytes, retrieval.canonical_bytes(trace))
        self.assertEqual(
            publication["rank_trace_sha256"],
            hashlib.sha256(trace_bytes).hexdigest(),
        )
        self.assertGreaterEqual(
            len(trace["contributions"]),
            sum(len(item["ranks"]) for item in self.pack["rank_contributions"]),
        )
        self.assertTrue(trace["channels"]["fts"])
        self.assertNotIn("results", self.pack["channels"]["fts"])
        preflight_bytes = publication["comparator_preflight_json"].encode("utf-8")
        preflight = json.loads(preflight_bytes)
        self.assertEqual(preflight_bytes, retrieval.canonical_bytes(preflight))
        self.assertEqual(
            publication["comparator_preflight_sha256"],
            hashlib.sha256(preflight_bytes).hexdigest(),
        )
        self.assertEqual(
            preflight,
            retrieval._comparator_preflight(self.pack, self.policy),
        )
        receipt = retrieval.finalize_comparison(
            self.conn, self.pack, self.response, self.policy
        )
        self.assertEqual(receipt["rank_trace_sha256"], publication["rank_trace_sha256"])
        self.assertEqual(
            receipt["comparator_preflight_sha256"],
            publication["comparator_preflight_sha256"],
        )

    def test_pack_publication_rejects_update_and_delete(self):
        for statement, parameters in (
            (
                "UPDATE history_pack_publications "
                "SET rank_trace_json = '{}' WHERE publication_id = ?",
                (self.pack["pack_publication_id"],),
            ),
            (
                "UPDATE history_pack_publications "
                "SET comparator_preflight_json = '{}' WHERE publication_id = ?",
                (self.pack["pack_publication_id"],),
            ),
            (
                "DELETE FROM history_pack_publications WHERE publication_id = ?",
                (self.pack["pack_publication_id"],),
            ),
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(statement, parameters)

    def test_replay_rederives_fusion_from_full_channel_results(self):
        receipt = retrieval.finalize_comparison(
            self.conn, self.pack, self.response, self.policy
        )
        self.conn.execute("DROP TRIGGER history_pack_publication_update_guard")
        self.conn.execute(
            "DROP TRIGGER history_pack_publication_delete_guard"
        )
        publication = self.conn.execute(
            """
            SELECT rank_trace_json
            FROM history_pack_publications
            WHERE publication_id = ?
            """,
            (self.pack["pack_publication_id"],),
        ).fetchone()
        trace = json.loads(publication[0])
        dense = next(
            item
            for item in trace["channels"]["dense"]
            if item["candidate_id"]
            == self.pack["lineages"][0]["matches"][0]["candidate_id"]
        )
        dense["rank"] += 1000
        trace_bytes = retrieval.canonical_bytes(trace)
        self.conn.execute(
            """
            UPDATE history_pack_publications
            SET rank_trace_json = ?, rank_trace_sha256 = ?
            WHERE publication_id = ?
            """,
            (
                trace_bytes.decode("utf-8"),
                hashlib.sha256(trace_bytes).hexdigest(),
                self.pack["pack_publication_id"],
            ),
        )
        with self.assertRaisesRegex(
            retrieval.ReceiptReplayError, "fusion"
        ):
            retrieval.replay_receipt(
                self.conn, self.pack, receipt, self.policy
            )

    def test_hard_match_byte_and_comparator_preflight_bounds(self):
        history_store.append_rows(
            self.conn,
            [row("confidence shared candidate %02d" % index) for index in range(20)],
            {"run_id": "hard-bounds"},
        )
        projection.rebuild(self.conn, self.policy)
        pack = retrieval.build_pack(
            self.conn,
            dict(self.query, story="confidence shared candidate"),
            "duplicate_search",
            self.policy,
        )
        matches = sum(len(item["matches"]) for item in pack["lineages"])
        self.assertLessEqual(matches, self.policy["max_matches"])
        if pack["retrieval_status"] == "complete":
            self.assertLessEqual(
                len(retrieval.canonical_bytes(pack))
                + self.policy["adapter_wrapper_allowance"],
                self.policy["max_retrieval_tokens"],
            )
            publication = self.conn.execute(
                "SELECT comparator_preflight_json FROM history_pack_publications "
                "WHERE publication_id = ?",
                (pack["pack_publication_id"],),
            ).fetchone()
            self.assertTrue(json.loads(publication[0])["fits"])
        else:
            self.assertEqual(pack["retrieval_status"], "budget_exceeded")

    def test_evolution_pack_carries_typed_highest_and_current_versions(self):
        mapped = self.root / "mapped"
        mapped.mkdir()
        (mapped / "ledger.instance-id").write_text("mapped\n", encoding="utf-8")
        ledger = mapped / "ledger.tsv"
        common_prefix = "shared-prefix " * 8
        parent_story = common_prefix + "PARENT-MECHANISM"
        child_story = common_prefix + "CHILD-MECHANISM"
        ledger.write_bytes(
            HEADER
            + row(parent_story)
            + row(child_story)
        )
        evidence = mapped / "edge-evidence.json"
        evidence.write_text('{"verified":true}\n', encoding="utf-8")
        mapping = mapped / "mapping.json"
        mapping.write_text(
            json.dumps(
                {
                    "version": "lineage-mapping-v1",
                    "mappings": [{
                        "parent_row": 1,
                        "child_row": 2,
                        "relation_type": "evolved_from",
                        "authority": "manual_mapping",
                        "evidence_path": str(evidence),
                    }],
                }
            ),
            encoding="utf-8",
        )
        conn = history_store.connect(mapped / "history.sqlite3")
        history_store.init_schema(conn)
        try:
            plan = history_store.build_import_plan(
                {"ledger": ledger, "mapping_manifest": mapping}, mapped / ".ai-ideas"
            )
            history_store.commit_import_plan(conn, plan)
            projection.rebuild(conn, self.policy)
            pack = retrieval.build_pack(
                conn,
                {
                    "candidate_id": "mapped-query",
                    "story": parent_story,
                    "theme": "World Models",
                },
                "evolution_search",
                self.policy,
            )
            self.assertEqual(pack["retrieval_status"], "complete")
            lineage_matches = [
                match
                for lineage in pack["lineages"]
                for match in lineage["matches"]
                if match["channel"] == "lineage"
            ]
            self.assertEqual(
                {match["version_role"] for match in lineage_matches},
                {"highest_match", "current"},
            )
            self.assertTrue(
                all(
                    match["relation_type"] == "evolved_from"
                    and match["edge_evidence_artifact_id"]
                    and match["material_delta"]
                    for match in lineage_matches
                )
            )
            self.assertTrue(
                all(
                    "PARENT-MECHANISM" in match["material_delta"]
                    and "CHILD-MECHANISM" in match["material_delta"]
                    and len(match["material_delta"])
                    <= retrieval.EVIDENCE_SPAN_LIMIT
                    for match in lineage_matches
                )
            )
            self.assertEqual(
                len({match["evidence_id"] for match in lineage_matches}),
                len(lineage_matches),
            )
            self.assertLessEqual(len(lineage_matches), 2)
        finally:
            conn.close()

    def test_query_schema_rejects_unknown_and_oversized_history_fields(self):
        for query in (
            dict(self.query, ledger_rows=["secret"]),
            dict(self.query, story="x" * 20000),
        ):
            with self.subTest(keys=sorted(query)):
                with self.assertRaises((ValueError, retrieval.RetrievalError)):
                    retrieval.build_pack(
                        self.conn, query, "duplicate_search", self.policy
                    )
        query_path = self.root / "bad-query.json"
        query_path.write_text(
            json.dumps(dict(self.query, ledger_rows=["secret"])), encoding="utf-8"
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "lib/history_cli.py"),
                "--db",
                str(self.db),
                "retrieve",
                "--policy",
                str(ROOT / "history/retrieval-policy-v1.json"),
                "--query",
                str(query_path),
                "--intent",
                "duplicate_search",
            ],
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_expansion_requires_prior_validated_receipt_and_round_bound(self):
        lineage_id = self.pack["lineages"][0]["lineage_id"]
        raw = {"lineage_ids": [lineage_id], "round": 1}
        with self.assertRaises(retrieval.RetrievalError):
            retrieval.build_pack(
                self.conn,
                self.query,
                "duplicate_search",
                self.policy,
                expansion_request=raw,
            )
        uncertain = copy.deepcopy(self.response)
        uncertain["status"] = "uncertain"
        uncertain["relations"][0]["relation"] = "uncertain"
        uncertain["expansion_request"] = {"lineage_ids": [lineage_id]}
        receipt = retrieval.finalize_comparison(
            self.conn, self.pack, uncertain, self.policy
        )
        request = {
            "lineage_ids": [lineage_id],
            "round": 1,
            "prior_pack_publication_id": self.pack["pack_publication_id"],
            "comparison_receipt_id": receipt["receipt_id"],
        }
        expanded = retrieval.build_pack(
            self.conn,
            self.query,
            "duplicate_search",
            self.policy,
            expansion_request=request,
        )
        self.assertEqual(expanded["expansion_round"], 1)
        self.assertEqual(
            expanded["prior_pack_publication_id"],
            self.pack["pack_publication_id"],
        )
        with self.assertRaises(retrieval.RetrievalError):
            retrieval.build_pack(
                self.conn,
                self.query,
                "duplicate_search",
                self.policy,
                expansion_request=dict(request, round=2),
            )

    def test_expansion_replays_prior_receipt_before_use(self):
        lineage_id = self.pack["lineages"][0]["lineage_id"]
        uncertain = copy.deepcopy(self.response)
        uncertain["status"] = "uncertain"
        uncertain["relations"][0]["relation"] = "uncertain"
        uncertain["expansion_request"] = {"lineage_ids": [lineage_id]}
        receipt = retrieval.finalize_comparison(
            self.conn, self.pack, uncertain, self.policy
        )
        corrupted = dict(receipt, comparison_sha256="0" * 64)
        self.conn.execute(
            """
            UPDATE history_receipts
            SET receipt_json = ?
            WHERE receipt_id = ?
            """,
            (
                retrieval.canonical_bytes(corrupted).decode("utf-8").rstrip("\n"),
                receipt["receipt_id"],
            ),
        )
        with self.assertRaises(retrieval.RetrievalError):
            retrieval.build_pack(
                self.conn,
                self.query,
                "duplicate_search",
                self.policy,
                expansion_request={
                    "lineage_ids": [lineage_id],
                    "round": 1,
                    "prior_pack_publication_id": self.pack["pack_publication_id"],
                    "comparison_receipt_id": receipt["receipt_id"],
                },
            )

    def test_lineage_results_bind_typed_edge_current_best_and_delta(self):
        appended = history_store.append_rows(
            self.conn,
            [row("confidence world model unsafe rollout")],
            {"run_id": "typed-lineage"},
        )
        child_id = appended["candidate_ids"][0]
        parent_id = self.conn.execute(
            """
            SELECT candidate_id
            FROM candidates
            WHERE lineage_id = (SELECT lineage_id FROM candidates WHERE candidate_id = ?)
              AND candidate_id != ?
            ORDER BY source_sequence
            LIMIT 1
            """,
            (child_id, child_id),
        ).fetchone()[0]
        artifact_id = "typed-lineage-evidence"
        self.conn.execute(
            """
            INSERT INTO artifacts(
              artifact_id, kind, state, sha256, byte_count, source_path,
              source_sequence, provenance_json, idempotency_key
            ) VALUES(?, 'lineage-evidence', 'installed', ?, 1, ?, 1, '{}', ?)
            """,
            (artifact_id, "7" * 64, "evidence/typed", artifact_id),
        )
        history_store.add_lineage_edge(
            self.conn,
            parent_id,
            child_id,
            "evolved_from",
            artifact_id,
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
        self.assertIn(
            "highest_match",
            {part for item in lineage_matches
             for part in item["version_role"].split("+")},
        )
        self.assertIn(
            "current",
            {part for item in lineage_matches
             for part in item["version_role"].split("+")},
        )
        self.assertTrue(
            all(item["relation_type"] == "evolved_from" for item in lineage_matches)
        )
        self.assertTrue(all(item["edge_evidence_artifact_id"] for item in lineage_matches))
        self.assertTrue(any(item["material_delta"] for item in lineage_matches))

    def test_replay_revalidates_candidate_status_and_relation_semantics(self):
        receipt = retrieval.finalize_comparison(
            self.conn, self.pack, self.response, self.policy
        )
        for mutation in (
            {"query_candidate_id": "different-query"},
            {"status": "uncertain"},
        ):
            changed = copy.deepcopy(receipt)
            changed.update(mutation)
            self._rehash_receipt(changed)
            with self.subTest(mutation=mutation):
                with self.assertRaises(retrieval.ReceiptReplayError):
                    retrieval.replay_receipt(
                        self.conn, self.pack, changed, self.policy
                    )

    def test_expansion_request_requires_uncertain_status_on_finalize_and_replay(self):
        lineage_id = self.pack["lineages"][0]["lineage_id"]
        expansion_request = {"lineage_ids": [lineage_id]}
        responses = []
        complete_match = copy.deepcopy(self.response)
        complete_match["expansion_request"] = expansion_request
        responses.append(complete_match)
        complete_no_match = copy.deepcopy(self.response)
        complete_no_match["status"] = "complete_no_match"
        complete_no_match["relations"][0]["relation"] = "distinct"
        complete_no_match["expansion_request"] = expansion_request
        responses.append(complete_no_match)
        for response in responses:
            with self.subTest(status=response["status"], operation="finalize"):
                with self.assertRaisesRegex(
                    retrieval.ComparisonValidationError,
                    "expansion requires uncertain status",
                ):
                    retrieval.finalize_comparison(
                        self.conn, self.pack, response, self.policy
                    )

            replay_response = copy.deepcopy(response)
            replay_response["expansion_request"] = None
            valid = retrieval.finalize_comparison(
                self.conn, self.pack, replay_response, self.policy
            )
            forged = copy.deepcopy(valid)
            forged["expansion_request"] = expansion_request
            forged["comparison_sha256"] = hashlib.sha256(
                retrieval.canonical_bytes(response)
            ).hexdigest()
            self._rehash_receipt(forged)
            with self.subTest(status=response["status"], operation="replay"):
                with self.assertRaisesRegex(
                    retrieval.ReceiptReplayError,
                    "expansion requires uncertain status",
                ):
                    retrieval.replay_receipt(
                        self.conn, self.pack, forged, self.policy
                    )

    def test_replay_survives_clean_rebuild_without_generation_id_reuse(self):
        receipt = retrieval.finalize_comparison(
            self.conn, self.pack, self.response, self.policy
        )
        first_generation = self.pack["index_generation"]
        first_manifest = self.pack["generation_manifest_sha256"]
        history_store.append_rows(
            self.conn, [row("later canonical candidate")], {"run_id": "later"}
        )
        projection.rebuild(self.conn, self.policy)
        projection.drop_rebuildable_projections(self.conn)
        rebuilt = projection.rebuild(self.conn, self.policy)
        self.assertGreater(rebuilt["index_generation"], first_generation)
        provenance = self.conn.execute(
            "SELECT manifest_sha256 FROM history_generation_provenance "
            "WHERE generation = ?",
            (first_generation,),
        ).fetchone()
        self.assertEqual(provenance[0], first_manifest)
        verified = retrieval.replay_receipt(
            self.conn, self.pack, receipt, self.policy
        )
        self.assertTrue(verified["verified"])

    def test_generation_sequence_recovers_existing_identity_on_upgrade(self):
        first_generation = self.pack["index_generation"]
        self.conn.execute(
            "DELETE FROM schema_meta WHERE key = 'history_index_generation_sequence'"
        )
        history_store.init_schema(self.conn)
        sequence = int(
            self.conn.execute(
                """
                SELECT value
                FROM schema_meta
                WHERE key = 'history_index_generation_sequence'
                """
            ).fetchone()[0]
        )
        self.assertGreaterEqual(sequence, first_generation)
        history_store.append_rows(
            self.conn,
            [row("upgrade sequence must not reuse a generation")],
            {"run_id": "generation-sequence-upgrade"},
        )
        rebuilt = projection.rebuild(self.conn, self.policy)
        self.assertGreater(rebuilt["index_generation"], first_generation)

    def test_receipt_table_rejects_invalid_intent_and_status(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO history_receipts(
                  receipt_id, query_candidate_id, intent, pack_sha256,
                  retrieval_policy_version, source_watermark, index_generation,
                  comparator_version, status, receipt_json, created_at
                ) VALUES('bad','q','bad-intent',?, ?, 1, 1, 'v',
                         'bad-status','{}',datetime('now'))
                """,
                ("0" * 64, self.policy["retrieval_policy_version"]),
            )


if __name__ == "__main__":
    unittest.main()
