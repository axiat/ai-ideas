#!/usr/bin/env python3
"""Round-two adversarial contracts for bounded history retrieval."""

import copy
import hashlib
import json
import pathlib
import pickle
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_projection as projection
from lib import history_retrieval as retrieval
from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
COMPARATOR_ROLE_IDENTITY = "tests/fixtures/history-compare-role.md"
COMPARATOR_ROLE_BYTES = (
    "Classify every retained lineage.\n"
    "Preserve exact evidence identifiers. \u03b4\n"
).encode("utf-8")


def build_pack(*args, **kwargs):
    kwargs.setdefault("comparator_role_bytes", COMPARATOR_ROLE_BYTES)
    kwargs.setdefault("comparator_role_identity", COMPARATOR_ROLE_IDENTITY)
    return retrieval.build_pack(*args, **kwargs)


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
        self.pack = build_pack(
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

        self.assertTrue(
            retrieval.permits_permanent_conclusion(self.conn, permanent)
        )
        self.assertFalse(
            retrieval.permits_permanent_conclusion(self.conn, uncertain)
        )
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
        self.assertTrue(
            retrieval.permits_permanent_conclusion(self.conn, permanent)
        )
        self.assertFalse(
            retrieval.permits_permanent_conclusion(self.conn, uncertain)
        )

    def test_verified_receipt_constructor_bypasses_do_not_forge_capability(self):
        for label, constructor in (
            (
                "base-tuple",
                lambda: tuple.__new__(
                    retrieval.VerifiedReceipt,
                    (
                        True,
                        True,
                        "forged",
                        "complete_match",
                        "forged-publication",
                    ),
                ),
            ),
            (
                "base-object",
                lambda: object.__new__(retrieval.VerifiedReceipt),
            ),
        ):
            with self.subTest(constructor=label):
                try:
                    forged = constructor()
                except TypeError:
                    continue
                self.assertFalse(
                    retrieval.permits_permanent_conclusion(self.conn, forged)
                )

    def test_capability_copy_subclass_and_durable_tamper_preserve_authority(self):
        receipt, capability = self._verify(self.match_response)
        attempts = (
            ("copy", lambda: copy.copy(capability)),
            ("deepcopy", lambda: copy.deepcopy(capability)),
            ("pickle", lambda: pickle.loads(pickle.dumps(capability))),
        )
        for label, operation in attempts:
            with self.subTest(operation=label):
                try:
                    duplicated = operation()
                except (TypeError, pickle.PickleError, AttributeError):
                    continue
                self.assertEqual(
                    retrieval.permits_permanent_conclusion(
                        self.conn, duplicated
                    ),
                    duplicated is capability,
                )

        class ForgedSubclass(retrieval.VerifiedReceipt):
            pass

        forged_subclass = object.__new__(ForgedSubclass)
        self.assertFalse(
            retrieval.permits_permanent_conclusion(
                self.conn, forged_subclass
            )
        )

        with self.assertRaisesRegex(
            Exception, "history receipt is immutable"
        ):
            self.conn.execute(
                """
                UPDATE history_receipts
                SET status = 'uncertain'
                WHERE receipt_id = ?
                """,
                (receipt["receipt_id"],),
            )
        with self.assertRaisesRegex(
            Exception, "history receipt is immutable"
        ):
            self.conn.execute(
                "DELETE FROM history_receipts WHERE receipt_id = ?",
                (receipt["receipt_id"],),
            )

        self.conn.execute(
            "DROP TRIGGER IF EXISTS history_receipt_update_guard"
        )
        self.conn.execute(
            """
            UPDATE history_receipts
            SET status = 'uncertain'
            WHERE receipt_id = ?
            """,
            (receipt["receipt_id"],),
        )
        self.assertFalse(
            retrieval.permits_permanent_conclusion(self.conn, capability)
        )


class ClassificationCoverageTests(RoundTwoFixture):
    @staticmethod
    def _classification_pack():
        lineages = []
        relations = []
        for index in (1, 2):
            lineage_id = "classification-lineage-%d" % index
            candidate_id = "classification-candidate-%d" % index
            evidence_id = "classification-evidence-%d" % index
            lineages.append(
                {
                    "lineage_id": lineage_id,
                    "matches": [{
                        "candidate_id": candidate_id,
                        "lineage_id": lineage_id,
                        "facet": "problem_estimand",
                        "evidence_id": evidence_id,
                    }],
                }
            )
            relations.append(
                {
                    "relation": "distinct",
                    "candidate_id": candidate_id,
                    "lineage_id": lineage_id,
                    "facet": "problem_estimand",
                    "evidence_id": evidence_id,
                    "material_difference": "different",
                    "confidence": 1.0,
                }
            )
        return {
            "intent": "duplicate_search",
            "lineages": lineages,
        }, relations

    @staticmethod
    def _response(status, relations):
        return {
            "status": status,
            "comparator_version": retrieval.COMPARATOR_VERSION,
            "relations": relations,
            "expansion_request": None,
        }

    def test_nonempty_pack_cannot_authorize_empty_complete_no_match(self):
        response = self._response("complete_no_match", [])
        with self.assertRaises(retrieval.ComparisonValidationError):
            retrieval.finalize_comparison(
                self.conn, self.pack, response, self.policy
            )

    def test_each_retained_lineage_requires_one_unique_classification(self):
        pack, relations = self._classification_pack()
        duplicate = [relations[0], dict(relations[0])]
        with self.assertRaisesRegex(
            retrieval.ComparisonValidationError,
            "lineage classification coverage mismatch",
        ):
            retrieval._validate_response(
                pack, self._response("complete_no_match", duplicate)
            )

    def test_complete_match_no_match_and_uncertain_statuses_are_coherent(self):
        pack, relations = self._classification_pack()

        complete_match = [dict(item) for item in relations]
        complete_match[0]["relation"] = "same_core_idea"
        retrieval._validate_response(
            pack, self._response("complete_match", complete_match)
        )

        retrieval._validate_response(
            pack, self._response("complete_no_match", relations)
        )

        uncertain = [dict(item) for item in relations]
        uncertain[0]["relation"] = "uncertain"
        retrieval._validate_response(
            pack, self._response("uncertain", uncertain)
        )

        incoherent = [dict(item) for item in uncertain]
        with self.assertRaisesRegex(
            retrieval.ComparisonValidationError,
            "complete match contains uncertain classification",
        ):
            retrieval._validate_response(
                pack, self._response("complete_match", incoherent)
            )


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
        pending = build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(pending["retrieval_status"], "partial")
        self.assertEqual(
            pending["index_generation"], self.pack["index_generation"]
        )

        rebuilt = projection.rebuild(self.conn, self.policy)
        published = build_pack(
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
        excluded_pending = build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(excluded_pending["retrieval_status"], "partial")
        projection.rebuild(self.conn, self.policy)
        excluded = build_pack(
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
        supersession_pending = build_pack(
            self.conn, self.query, "evolution_search", self.policy
        )
        self.assertEqual(supersession_pending["retrieval_status"], "partial")
        projection.rebuild(self.conn, self.policy)
        supersession = build_pack(
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

    def test_supersedes_edge_deactivates_parent_without_manual_exclusion(self):
        baseline_receipt = retrieval.finalize_comparison(
            self.conn, self.pack, self.match_response, self.policy
        )
        appended = history_store.append_rows(
            self.conn,
            [ledger_row("confidence world model unsafe rollout")],
            {"run_id": "round-two-derived-supersession"},
        )
        child_id = appended["candidate_ids"][0]
        projection.rebuild(self.conn, self.policy)
        self._install_edge_artifact("round-two-derived-supersession-edge")
        history_store.add_lineage_edge(
            self.conn,
            self.candidate_id,
            child_id,
            "supersedes",
            "round-two-derived-supersession-edge",
            "explicit",
        )

        pending = build_pack(
            self.conn, self.query, "duplicate_search", self.policy
        )
        self.assertEqual(pending["retrieval_status"], "partial")

        projection.rebuild(self.conn, self.policy)
        active = {
            row["candidate_id"]: row["active"]
            for row in self.conn.execute(
                """
                SELECT candidate_id, active
                FROM search_index_entries
                WHERE candidate_id IN (?, ?)
                """,
                (self.candidate_id, child_id),
            )
        }
        self.assertEqual(active[self.candidate_id], 0)
        self.assertEqual(active[child_id], 1)
        self.assertEqual(
            projection.exact_lookup(
                self.conn, self.query["story"], self.policy["per_channel_depth"]
            ),
            [child_id],
        )

        verified = retrieval.replay_receipt(
            self.conn, self.pack, baseline_receipt, self.policy
        )
        self.assertTrue(verified["verified"])

    def test_dense_evidence_uses_published_text_not_live_facets(self):
        baseline = build_pack(
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
        repeated = build_pack(
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

    def test_overflowed_evolution_units_preserve_truthful_channel_audit(self):
        rows = []
        for index in range(10):
            story = "shared mechanism lineage %d" % index
            rows.extend((ledger_row(story), ledger_row(story)))
        appended = history_store.append_rows(
            self.conn,
            rows,
            {"run_id": "round-two-unit-overflow"},
        )
        projection.rebuild(self.conn, self.policy)
        for index in range(10):
            artifact_id = "round-two-unit-overflow-%d" % index
            self._install_edge_artifact(artifact_id)
            history_store.add_lineage_edge(
                self.conn,
                appended["candidate_ids"][index * 2],
                appended["candidate_ids"][index * 2 + 1],
                "evolved_from",
                artifact_id,
                "explicit",
            )
        projection.rebuild(self.conn, self.policy)

        trace = {}
        pack = retrieval._build_pack_snapshot(
            self.conn,
            {
                "candidate_id": "round-two-unit-overflow-query",
                "story": "shared mechanism lineage",
                "theme": "World Models",
            },
            "evolution_search",
            self.policy,
            trace_sink=trace,
        )

        self.assertEqual(pack["retrieval_status"], "budget_exceeded")
        self.assertFalse(pack["lineages"])
        self.assertGreaterEqual(
            pack["omitted_lineage_count"], 6
        )
        self.assertEqual(
            pack["omitted_lineage_count"],
            len(trace["fusion"]["lineage_order"]),
        )
        for channel in ("exact", "fts", "dense", "lineage"):
            with self.subTest(channel=channel):
                self.assertEqual(pack["channels"][channel]["status"], "complete")
                self.assertEqual(
                    pack["channels"][channel]["result_count"],
                    len(trace["channels"][channel]),
                )
        self.assertGreater(pack["channels"]["lineage"]["result_count"], 10)

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
        pack = build_pack(
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

    def _version_pair(self, run_id):
        appended = history_store.append_rows(
            self.conn,
            [ledger_row("confidence world model unsafe rollout")],
            {"run_id": run_id},
        )
        current_id = appended["candidate_ids"][0]
        edge_id = run_id + "-edge"
        self._install_edge_artifact(edge_id)
        history_store.add_lineage_edge(
            self.conn,
            self.candidate_id,
            current_id,
            "evolved_from",
            edge_id,
            "explicit",
        )
        projection.rebuild(self.conn, self.policy)
        return (
            retrieval._candidate_row(self.conn, self.candidate_id),
            retrieval._candidate_row(self.conn, current_id),
        )

    def _assert_prelineage_highest(self, exact, fts, dense):
        semantic_results = {
            "exact": exact,
            "fts": fts,
            "dense": dense,
            "expansion": [],
            "lineage": [],
        }
        ranked, contributions = retrieval._fuse(
            semantic_results, self.policy
        )
        candidate_order = retrieval._fusion_summary(
            ranked, contributions
        )["candidate_order"]
        lineage_id = retrieval._candidate_row(
            self.conn, self.candidate_id
        )["lineage_id"]
        expected = next(
            item["candidate_id"]
            for item in candidate_order
            if item["lineage_id"] == lineage_id
        )
        with (
            mock.patch.object(
                retrieval, "_exact_channel", return_value=exact
            ),
            mock.patch.object(
                retrieval, "_fts_channel", return_value=fts
            ),
            mock.patch.object(
                retrieval, "_dense_channel", return_value=dense
            ),
        ):
            pack = retrieval._build_pack_snapshot(
                self.conn,
                self.query,
                "evolution_search",
                self.policy,
            )
        highest = {
            match["candidate_id"]
            for lineage in pack["lineages"]
            for match in lineage["matches"]
            if "highest_match" in match.get("version_role", "").split("+")
        }
        self.assertEqual(highest, {expected})
        return expected

    def test_highest_match_uses_prelineage_semantic_rrf(self):
        older, current = self._version_pair(
            "round-three-fused-highest"
        )
        fts_results = [
            retrieval._evidence(
                older,
                "fts",
                "claimed_delta",
                1.0,
                1,
                older["story"],
            ),
            retrieval._evidence(
                current,
                "fts",
                "claimed_delta",
                0.5,
                2,
                current["story"],
            ),
            retrieval._evidence(
                current,
                "fts",
                "problem_estimand",
                1.0,
                1,
                current["story"],
            ),
        ]
        self.assertEqual(
            self._assert_prelineage_highest([], fts_results, []),
            current["candidate_id"],
        )

    def test_highest_match_prelineage_rrf_includes_exact_hits(self):
        older, current = self._version_pair(
            "round-three-fused-exact"
        )
        exact = [
            retrieval._evidence(
                older,
                "exact",
                "problem_estimand",
                1.0,
                1,
                older["story"],
            )
        ]
        fts = [
            retrieval._evidence(
                current,
                "fts",
                "claimed_delta",
                0.5,
                2,
                current["story"],
            )
        ]
        self.assertEqual(
            self._assert_prelineage_highest(exact, fts, []),
            older["candidate_id"],
        )

    def test_highest_match_prelineage_rrf_ties_by_candidate_id(self):
        older, current = self._version_pair(
            "round-three-fused-tie"
        )
        fts = [
            retrieval._evidence(
                row,
                "fts",
                "claimed_delta",
                1.0,
                1,
                row["story"],
            )
            for row in (older, current)
        ]
        self.assertEqual(
            self._assert_prelineage_highest([], fts, []),
            min(older["candidate_id"], current["candidate_id"]),
        )

    def test_multihop_evolution_keeps_complete_highest_to_current_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            ledger = root / "ledger.tsv"
            stories = [
                "stage-A-mechanism",
                "stage-B-mechanism",
                "stage-C-mechanism",
                "stage-D-mechanism",
            ]
            ledger.write_bytes(
                HEADER + b"".join(ledger_row(story) for story in stories)
            )
            (root / "ledger.instance-id").write_text(
                "round-two-multihop\n", encoding="utf-8"
            )
            mappings = []
            for index in range(3):
                artifact = root / ("edge-%d.json" % index)
                artifact.write_text('{"verified":true}\n', encoding="utf-8")
                mappings.append(
                    {
                        "parent_row": index + 1,
                        "child_row": index + 2,
                        "relation_type": "evolved_from",
                        "authority": "manual_mapping",
                        "evidence_path": str(artifact),
                    }
                )
            mapping = root / "mapping.json"
            mapping.write_text(
                json.dumps(
                    {"version": "lineage-mapping-v1", "mappings": mappings}
                ),
                encoding="utf-8",
            )
            conn = history_store.connect(root / "history.sqlite3")
            try:
                history_store.init_schema(conn)
                plan = history_store.build_import_plan(
                    {
                        "ledger": ledger,
                        "mapping_manifest": mapping,
                    },
                    root / ".ai-ideas",
                )
                history_store.commit_import_plan(conn, plan)
                projection.rebuild(conn, self.policy)
                candidates = conn.execute(
                    """
                    SELECT candidate_id, story
                    FROM candidates
                    ORDER BY source_sequence
                    """
                ).fetchall()
                candidate_ids = [row["candidate_id"] for row in candidates]
                pack = build_pack(
                    conn,
                    {
                        "candidate_id": "round-two-multihop-query",
                        "story": stories[1],
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
                    [
                        (
                            match["parent_candidate_id"],
                            match["child_candidate_id"],
                            match["candidate_id"],
                            match["edge_direction"],
                        )
                        for match in lineage_matches
                    ],
                    [
                        (
                            candidate_ids[1],
                            candidate_ids[2],
                            candidate_ids[1],
                            "parent",
                        ),
                        (
                            candidate_ids[2],
                            candidate_ids[3],
                            candidate_ids[3],
                            "child",
                        ),
                    ],
                )
                self.assertTrue(
                    all(
                        left in match["material_delta"]
                        and right in match["material_delta"]
                        for match, left, right in zip(
                            lineage_matches,
                            stories[1:3],
                            stories[2:4],
                        )
                    )
                )
            finally:
                conn.close()


class PublicationAuditTests(RoundTwoFixture):
    def test_canonical_invocation_binds_explicit_role_bytes_and_identity(self):
        invocation_bytes = retrieval.comparator_invocation_bytes(
            self.pack,
            self.policy,
            role_bytes=COMPARATOR_ROLE_BYTES,
            role_identity=COMPARATOR_ROLE_IDENTITY,
        )
        invocation = json.loads(invocation_bytes)
        self.assertEqual(
            invocation["fixed_instructions"],
            COMPARATOR_ROLE_BYTES.decode("utf-8"),
        )
        self.assertEqual(
            invocation["receipts"],
            [{
                "pack_publication_id": self.pack["pack_publication_id"],
                "role_identity": COMPARATOR_ROLE_IDENTITY,
                "role_sha256": hashlib.sha256(
                    COMPARATOR_ROLE_BYTES
                ).hexdigest(),
            }],
        )

    def test_publication_preflight_binds_canonical_invocation(self):
        publication = self.conn.execute(
            """
            SELECT * FROM history_pack_publications
            WHERE publication_id = ?
            """,
            (self.pack["pack_publication_id"],),
        ).fetchone()
        invocation_bytes = retrieval.comparator_invocation_bytes(
            self.pack,
            self.policy,
            role_bytes=COMPARATOR_ROLE_BYTES,
            role_identity=COMPARATOR_ROLE_IDENTITY,
        )
        invocation = json.loads(invocation_bytes)
        self.assertEqual(
            invocation_bytes, retrieval.canonical_bytes(invocation)
        )
        preflight_bytes = publication["comparator_preflight_json"].encode(
            "utf-8"
        )
        preflight = json.loads(preflight_bytes)
        stored_invocation_bytes = publication[
            "comparator_invocation_json"
        ].encode("utf-8")
        self.assertEqual(stored_invocation_bytes, invocation_bytes)
        self.assertEqual(
            publication["comparator_invocation_sha256"],
            hashlib.sha256(invocation_bytes).hexdigest(),
        )
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
        self.assertEqual(
            receipt["comparator_invocation_sha256"],
            publication["comparator_invocation_sha256"],
        )
        verified = retrieval.replay_receipt(
            self.conn, self.pack, receipt, self.policy
        )
        self.assertTrue(verified["verified"])

    def test_immutable_publication_binds_pack_trace_invocation_and_preflight(self):
        publication = self.conn.execute(
            """
            SELECT * FROM history_pack_publications
            WHERE publication_id = ?
            """,
            (self.pack["pack_publication_id"],),
        ).fetchone()
        invocation_bytes = publication[
            "comparator_invocation_json"
        ].encode("utf-8")
        trace_bytes = publication["rank_trace_json"].encode("utf-8")
        preflight_bytes = publication[
            "comparator_preflight_json"
        ].encode("utf-8")
        self.assertEqual(
            bytes(publication["pack_bytes"]),
            retrieval.canonical_bytes(self.pack),
        )
        self.assertEqual(
            publication["comparator_invocation_sha256"],
            hashlib.sha256(invocation_bytes).hexdigest(),
        )
        self.assertEqual(
            publication["rank_trace_sha256"],
            hashlib.sha256(trace_bytes).hexdigest(),
        )
        self.assertEqual(
            publication["comparator_preflight_sha256"],
            hashlib.sha256(preflight_bytes).hexdigest(),
        )
        with self.assertRaisesRegex(
            Exception, "history pack publication is immutable"
        ):
            self.conn.execute(
                """
                UPDATE history_pack_publications
                SET comparator_invocation_json = '{}'
                WHERE publication_id = ?
                """,
                (self.pack["pack_publication_id"],),
            )
        with self.assertRaisesRegex(
            retrieval.RetrievalError, "publication identity collision"
        ):
            retrieval._publish_pack(
                self.conn,
                self.pack,
                self.policy,
                json.loads(preflight_bytes),
                json.loads(trace_bytes),
                invocation_bytes + b" ",
            )


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
                        pack = build_pack(
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
                        self.assertTrue(
                            trace["fusion"]["lineage_order"]
                        )
                        self.assertEqual(
                            pack["omitted_lineage_count"],
                            len(trace["fusion"]["lineage_order"]),
                        )
                        for channel, evidence in trace["channels"].items():
                            self.assertEqual(
                                pack["channels"][channel]["result_count"],
                                len(evidence),
                            )
                        with self.assertRaisesRegex(
                            Exception,
                            "history pack publication is immutable",
                        ):
                            conn.execute(
                                """
                                UPDATE history_pack_publications
                                SET rank_trace_json = '{}'
                                WHERE publication_id = ?
                                """,
                                (pack["pack_publication_id"],),
                            )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
