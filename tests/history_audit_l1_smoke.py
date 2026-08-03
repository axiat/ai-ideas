#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib import history_audit as audit_module
from lib import history_audit_store as audit_store
from lib import history_contract_v2 as contract
from lib import history_projection as projection
from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
SHA = "0" * 64


def row(story):
    return (
        "2026-08-03\thunt\tAudit\t"
        + story
        + "\taccept-w-rev\treason\tlow\tdesign-fixable\n"
    ).encode("utf-8")


class _ZeroAudit:
    """Importable RED seam; every fallback is deliberately semantically wrong."""

    class ActivationCrash(RuntimeError):
        pass

    @staticmethod
    def freeze_snapshot(conn, *, run_id, batch_id, current_batch_ids):
        return {
            "run_id": run_id,
            "batch_id": batch_id,
            "snapshot_id": "missing",
            "snapshot_hash": SHA,
            "history_as_of_watermark": 0,
            "current_batch_id_namespace": "missing",
            "current_batch_ids": [],
            "current_batch_ids_hash": SHA,
            "exclusion_policy_sha": SHA,
            "expected_asset_ids": [],
            "expected_asset_ids_hash": SHA,
        }

    @staticmethod
    def read_frozen_assets(conn, snapshot):
        return [
            item[0]
            for item in conn.execute(
                "SELECT candidate_id FROM candidates ORDER BY candidate_id"
            )
        ]

    @staticmethod
    def stage_raw_batch(conn, *, snapshot, raw_candidates, direction_receipt):
        return {
            "run_id": snapshot["run_id"],
            "batch_id": snapshot["batch_id"],
            "candidates": list(raw_candidates),
        }

    @staticmethod
    def plan_batch_pairs(staged_batch):
        return {"pair_plan_sha": SHA, "pair_count": 0, "pairs": []}

    @staticmethod
    def record_batch_pair_results(conn, staged_batch, pair_plan, pair_results):
        return {"pair_plan_sha": SHA, "pair_result_sha": SHA, "pair_count": 0}

    @staticmethod
    def record_direction_check(conn, *, staged_candidate, direction_receipt,
                               semantic_relation, lineage_relation, evidence_sha):
        return {
            "staging_candidate_id": staged_candidate["staging_candidate_id"],
            "evidence_sha": SHA,
        }

    @staticmethod
    def activate_staged_candidate(*args, **kwargs):
        prior = args[0].execute(
            "SELECT candidate_id, source_sequence FROM candidates "
            "ORDER BY source_sequence LIMIT 1"
        ).fetchone()
        return {
            "legacy_candidate_id": prior[0],
            "source_sequence": prior[1],
            "replayed": False,
        }

    @staticmethod
    def derive_final_status(**kwargs):
        return "uncertain", "missing"

    @staticmethod
    def build_l1_receipt(snapshot, retrieval, adjudication, qualification):
        return {}

    @staticmethod
    def fair_family_fusion(channel_rankings, lineage_by_candidate):
        return {
            "lineages": [],
            "family_semantics": {"hash_dense": "missing"},
        }

    @staticmethod
    def select_l1_comparisons(fused, *, routine_cutoff):
        return {
            "selected_lineage_ids": [],
            "mandatory_lineage_ids": [],
            "routine_lineage_ids": [],
        }

    @staticmethod
    def evaluate_l1_coverage(selection, adjudicated_lineage_ids):
        return {
            "coverage_complete": True,
            "missing_mandatory_lineage_ids": [],
            "exhausted_reason": None,
        }

    @staticmethod
    def read_l1_rankings(conn, snapshot, query, *, depth):
        current = [
            row[0]
            for row in conn.execute(
                "SELECT candidate_id FROM candidates ORDER BY source_sequence"
            )
        ]
        return {
            "exact": [
                {"candidate_id": item, "query_view_id": "story", "score": 1.0}
                for item in current
            ],
            "fts": [],
            "hash_dense": [],
        }


class AuditAdapter:
    def __getattr__(self, name):
        return getattr(audit_module, name, getattr(_ZeroAudit, name))


audit = AuditAdapter()


class HistoryAuditL1Smoke(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text(
            "history-audit-l1\n", encoding="utf-8"
        )
        self.ledger = self.root / "ledger.tsv"
        self.ledger.write_bytes(HEADER + row("prior alpha") + row("prior beta"))
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        history_store.import_tsv_epoch(self.conn, self.ledger)
        audit_store.init_schema(self.conn)
        self.run_id = "run-l1"
        self.batch_id = "batch-l1"
        self.plan_hash = "1" * 64
        self.conn.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id, manifest_schema_version, plan_hash, manifest_json, created_at
            ) VALUES(?, 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-03T00:00:00Z')
            """,
            (self.run_id, self.plan_hash),
        )
        self.raw = [row("batch gamma"), row("batch delta")]
        self.staging_ids = (
            "stg-v2-" + "2" * 64,
            "stg-v2-" + "3" * 64,
        )
        self.direction = {
            "direction_id": "direction-l1",
            "contract_sha": "4" * 64,
            "validator_version": "direction-validator-v1",
            "artifact_sha": "5" * 64,
        }

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _snapshot(self, ids=None):
        return audit.freeze_snapshot(
            self.conn,
            run_id=self.run_id,
            batch_id=self.batch_id,
            current_batch_ids=list(self.staging_ids if ids is None else ids),
        )

    def _staged(self, snapshot=None, raw=None, ids=None):
        snapshot = snapshot or self._snapshot(ids=ids)
        raw = self.raw if raw is None else raw
        ids = self.staging_ids if ids is None else ids
        candidates = [
            {"staging_candidate_id": staging_id, "raw_candidate": raw_value}
            for staging_id, raw_value in zip(ids, raw)
        ]
        return audit.stage_raw_batch(
            self.conn,
            snapshot=snapshot,
            raw_candidates=candidates,
            direction_receipt=self.direction,
        )

    def _activation_evidence(self, staged):
        pair_plan = audit.plan_batch_pairs(staged)
        pair_results = [
            {
                "left_staging_candidate_id": pair["left_staging_candidate_id"],
                "right_staging_candidate_id": pair["right_staging_candidate_id"],
                "semantic_relation": "distinct",
                "evidence_sha": hashlib.sha256(
                    (pair["left_staging_candidate_id"] + pair["right_staging_candidate_id"]).encode()
                ).hexdigest(),
            }
            for pair in pair_plan["pairs"]
        ]
        pair_receipt = audit.record_batch_pair_results(
            self.conn, staged, pair_plan, pair_results
        )
        direction_check = audit.record_direction_check(
            self.conn,
            staged_candidate=staged["candidates"][0],
            direction_receipt=self.direction,
            semantic_relation="distinct",
            lineage_relation="none",
            evidence_sha="6" * 64,
        )
        return pair_plan, pair_receipt, direction_check

    def _foreign_snapshot(self):
        self.conn.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id, manifest_schema_version, plan_hash, manifest_json, created_at
            ) VALUES('run-foreign', 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-03T00:00:00Z')
            """,
            ("c" * 64,),
        )
        return audit.freeze_snapshot(
            self.conn,
            run_id="run-foreign",
            batch_id="batch-foreign",
            current_batch_ids=["stg-v2-" + "d" * 64],
        )

    def _insert_foreign_pair_receipt(self, staged, foreign_snapshot):
        pair_plan = audit.plan_batch_pairs(staged)
        pair_result_sha = "e" * 64
        self.conn.execute(
            """
            INSERT INTO audit_batch_pair_receipts(
              run_id, batch_id, snapshot_id, pair_plan_sha, pair_result_sha,
              pair_count, completed_at
            ) VALUES(?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
            """,
            (
                staged["run_id"], staged["batch_id"],
                foreign_snapshot["snapshot_id"], pair_plan["pair_plan_sha"],
                pair_result_sha, pair_plan["pair_count"],
            ),
        )
        for pair in pair_plan["pairs"]:
            self.conn.execute(
                """
                INSERT INTO audit_batch_pairs(
                  run_id, batch_id, left_staging_candidate_id,
                  right_staging_candidate_id, pair_plan_sha, pair_result_sha,
                  created_at
                ) VALUES(?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
                """,
                (
                    staged["run_id"], staged["batch_id"],
                    pair["left_staging_candidate_id"],
                    pair["right_staging_candidate_id"],
                    pair_plan["pair_plan_sha"], pair_result_sha,
                ),
            )
        return {
            "run_id": staged["run_id"],
            "batch_id": staged["batch_id"],
            "snapshot_id": foreign_snapshot["snapshot_id"],
            "pair_plan_sha": pair_plan["pair_plan_sha"],
            "pair_result_sha": pair_result_sha,
            "pair_count": pair_plan["pair_count"],
        }

    def _receipt_inputs(self, snapshot):
        prior_ids = snapshot["expected_asset_ids"]
        retrieval = {
            "run_id": self.run_id,
            "plan_hash": self.plan_hash,
            "candidate_hash": "7" * 64,
            "observed_asset_ids_hash": contract.ordered_set_sha256(
                "history-observed-assets-v2", prior_ids
            ),
            "missing_ids": [],
            "duplicate_ids": [],
            "extra_ids": [],
            "invalid_schema": False,
            "invalid_anchor": False,
            "truncated": False,
            "provider_pools_ordered": {
                "comparator": ["fake"],
                "map": ["fake"],
                "detail": ["fake"],
                "reduce": ["fake"],
            },
            "provider_capability_profile_hashes": ["8" * 64],
            "capacity_profile_id": "fake-capacity-v1",
            "risk_policy_version": "risk-v1",
            "matched_router_rule_ids": [],
            "settlement_policy_sha": "9" * 64,
            "shard_plan_sha": "a" * 64,
            "logical_task_hashes": [],
            "attempt_manifest_hashes": [],
            "raw_request_output_cas_hashes": [],
            "minimum_receipt_sha": "b" * 64,
            "coverage_complete": True,
        }
        adjudication = {
            "adjudication_complete": True,
            "verified_hits": [],
            "unresolved_conflict": False,
            "exhausted_reason": None,
            "evidence_anchors": [],
        }
        qualification = {
            "semantic_policy_profile_id": "semantic-v1",
            "semantic_policy_qualified": False,
            "no_match_basis": None,
        }
        return retrieval, adjudication, qualification

    def test_snapshot_uses_source_sequence_and_excludes_current_batch_ids(self):
        snapshot = self._snapshot()
        prior = [
            row[0]
            for row in self.conn.execute(
                "SELECT candidate_id FROM candidates ORDER BY candidate_id"
            )
        ]
        self.assertEqual(snapshot["history_as_of_watermark"], 2)
        self.assertEqual(snapshot["expected_asset_ids"], prior)
        self.assertEqual(snapshot["current_batch_ids"], sorted(self.staging_ids))
        self.assertEqual(
            snapshot["current_batch_ids_hash"],
            contract.ordered_set_sha256(
                "history-current-batch-ids-v2", self.staging_ids
            ),
        )
        stored = self.conn.execute(
            "SELECT run_id, batch_id FROM audit_snapshots WHERE snapshot_id=?",
            (snapshot["snapshot_id"],),
        ).fetchone()
        self.assertEqual(tuple(stored), (self.run_id, self.batch_id))

    def test_concurrent_append_after_watermark_does_not_change_asset_root(self):
        snapshot = self._snapshot()
        expected = list(snapshot["expected_asset_ids"])
        history_store.append_rows(
            self.conn, [row("concurrent epsilon")], {"run_id": "other-run"}
        )
        self.assertEqual(audit.read_frozen_assets(self.conn, snapshot), expected)
        self.assertEqual(
            snapshot["expected_asset_ids_hash"],
            contract.ordered_set_sha256("history-snapshot-assets-v2", expected),
        )

    def test_batch_internal_pairs_exist_before_activation(self):
        before = self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
        staged = self._staged()
        plan = audit.plan_batch_pairs(staged)
        self.assertEqual(plan["pair_count"], 1)
        self.assertEqual(
            plan["pairs"][0]["comparison_kinds"], ["exact", "semantic"]
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0], before
        )

    def test_pair_persistence_rejects_snapshot_from_another_run_and_batch(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        foreign = self._foreign_snapshot()
        substituted = dict(staged, snapshot_id=foreign["snapshot_id"])
        pair_plan = audit.plan_batch_pairs(substituted)
        pair_results = [
            {
                "left_staging_candidate_id": pair["left_staging_candidate_id"],
                "right_staging_candidate_id": pair["right_staging_candidate_id"],
                "semantic_relation": "distinct",
                "evidence_sha": "f" * 64,
            }
            for pair in pair_plan["pairs"]
        ]
        with self.assertRaises(ValueError):
            audit.record_batch_pair_results(
                self.conn, substituted, pair_plan, pair_results
            )

    def test_activation_rejects_foreign_snapshot_even_if_storage_is_corrupt(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        foreign = self._foreign_snapshot()
        self.conn.execute(
            "DROP TRIGGER IF EXISTS audit_batch_pair_receipts_snapshot_owner_guard"
        )
        pair_receipt = self._insert_foreign_pair_receipt(staged, foreign)
        direction_check = audit.record_direction_check(
            self.conn,
            staged_candidate=staged["candidates"][0],
            direction_receipt=self.direction,
            semantic_relation="distinct",
            lineage_relation="none",
            evidence_sha="6" * 64,
        )
        before = self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
        with self.assertRaises(ValueError):
            audit.activate_staged_candidate(
                self.conn,
                snapshot=snapshot,
                staged_candidate=staged["candidates"][0],
                pair_receipt=pair_receipt,
                direction_check=direction_check,
            )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
            before,
        )

    def test_database_rejects_pair_receipt_with_foreign_snapshot_owner(self):
        staged = self._staged(snapshot=self._snapshot())
        foreign = self._foreign_snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_foreign_pair_receipt(staged, foreign)

    def test_pair_snapshot_owner_upgrade_probe_rejects_contradictory_rows(self):
        conn = history_store.connect(self.root / "upgrade.sqlite3")
        try:
            history_store.init_schema(conn)
            old_migrations = tuple(
                migration
                for migration in audit_store.MIGRATIONS
                if migration.component != "l1-pair-snapshot-ownership"
            )
            with mock.patch.object(audit_store, "MIGRATIONS", old_migrations):
                audit_store.init_schema(conn)
            for run_id, plan_hash in (("run-a", "1" * 64), ("run-b", "2" * 64)):
                conn.execute(
                    """
                    INSERT INTO audit_run_manifests(
                      run_id, manifest_schema_version, plan_hash,
                      manifest_json, created_at
                    ) VALUES(?, 'history-audit-manifest-v2', ?, '{}',
                             '2026-08-03T00:00:00Z')
                    """,
                    (run_id, plan_hash),
                )
            for snapshot_id, run_id, batch_id, marker in (
                ("3" * 64, "run-a", "batch-a", "4"),
                ("5" * 64, "run-b", "batch-b", "6"),
            ):
                conn.execute(
                    """
                    INSERT INTO audit_snapshots(
                      snapshot_id, snapshot_hash, history_as_of_watermark,
                      current_batch_id_namespace, current_batch_ids_hash,
                      exclusion_policy_sha, expected_asset_ids_hash, created_at,
                      run_id, batch_id
                    ) VALUES(?, ?, 0, 'history-v2-staging-v1', ?, ?, ?,
                             '2026-08-03T00:00:00Z', ?, ?)
                    """,
                    (
                        snapshot_id, marker * 64, "7" * 64, "8" * 64,
                        "9" * 64, run_id, batch_id,
                    ),
                )
            for source_order, staging_id in enumerate(
                ("stg-v2-" + "a" * 64, "stg-v2-" + "b" * 64)
            ):
                conn.execute(
                    """
                    INSERT INTO audit_batch_staging(
                      staging_candidate_id, run_id, batch_id, candidate_hash,
                      raw_artifact_sha, source_order, created_at
                    ) VALUES(?, 'run-a', 'batch-a', ?, ?, ?,
                             '2026-08-03T00:00:00Z')
                    """,
                    (staging_id, chr(ord("a") + source_order) * 64,
                     chr(ord("c") + source_order) * 64, source_order),
                )
            conn.execute(
                """
                INSERT INTO audit_batch_pair_receipts(
                  run_id, batch_id, snapshot_id, pair_plan_sha,
                  pair_result_sha, pair_count, completed_at
                ) VALUES('run-a', 'batch-a', ?, ?, ?, 1,
                         '2026-08-03T00:00:00Z')
                """,
                ("5" * 64, "d" * 64, "e" * 64),
            )
            with self.assertRaises(audit_store.AuditMigrationError):
                audit_store.init_schema(conn)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM audit_schema_migrations "
                    "WHERE component='l1-pair-snapshot-ownership'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_singleton_batch_records_empty_pair_receipt_and_activates(self):
        staging_id = self.staging_ids[0]
        snapshot = self._snapshot(ids=[staging_id])
        staged = self._staged(
            snapshot=snapshot, raw=[self.raw[0]], ids=[staging_id]
        )
        pair_plan = audit.plan_batch_pairs(staged)
        self.assertEqual(pair_plan["pair_count"], 0)
        pair_receipt = audit.record_batch_pair_results(
            self.conn, staged, pair_plan, []
        )
        self.assertEqual(pair_receipt["pair_count"], 0)
        direction_check = audit.record_direction_check(
            self.conn,
            staged_candidate=staged["candidates"][0],
            direction_receipt=self.direction,
            semantic_relation="distinct",
            lineage_relation="none",
            evidence_sha="6" * 64,
        )
        activated = audit.activate_staged_candidate(
            self.conn,
            snapshot=snapshot,
            staged_candidate=staged["candidates"][0],
            pair_receipt=pair_receipt,
            direction_check=direction_check,
        )
        self.assertGreater(
            activated["source_sequence"], snapshot["history_as_of_watermark"]
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM audit_batch_pairs").fetchone()[0],
            0,
        )

    def test_provider_local_ids_are_never_used_as_corpus_exclusions(self):
        with self.assertRaises(ValueError):
            self._snapshot(ids=["I1", "I2"])

    def test_staging_id_never_occupies_or_predicts_legacy_candidate_id(self):
        staged = self._staged()
        for item in staged["candidates"]:
            self.assertTrue(item["staging_candidate_id"].startswith("stg-v2-"))
            self.assertIsNone(
                self.conn.execute(
                    "SELECT 1 FROM candidates WHERE candidate_id=?",
                    (item["staging_candidate_id"],),
                ).fetchone()
            )
            self.assertNotIn("legacy_candidate_id", item)

    def test_activation_map_binds_existing_append_allocator_identity(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        _, pair_receipt, direction_check = self._activation_evidence(staged)
        result = audit.activate_staged_candidate(
            self.conn,
            snapshot=snapshot,
            staged_candidate=staged["candidates"][0],
            pair_receipt=pair_receipt,
            direction_check=direction_check,
        )
        candidate = self.conn.execute(
            "SELECT candidate_id, source_sequence FROM candidates WHERE candidate_id=?",
            (result["legacy_candidate_id"],),
        ).fetchone()
        self.assertEqual(tuple(candidate), (result["legacy_candidate_id"], result["source_sequence"]))
        self.assertGreater(result["source_sequence"], snapshot["history_as_of_watermark"])
        self.assertNotEqual(result["legacy_candidate_id"], staged["candidates"][0]["staging_candidate_id"])
        activation = self.conn.execute(
            "SELECT * FROM audit_activation_maps WHERE staging_candidate_id=?",
            (staged["candidates"][0]["staging_candidate_id"],),
        ).fetchone()
        self.assertIsNotNone(activation)
        self.assertEqual(activation["legacy_candidate_id"], result["legacy_candidate_id"])
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM search_projection_outbox WHERE record_id=?",
                (result["legacy_candidate_id"],),
            ).fetchone()[0],
            1,
        )
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM audit_activation_receipts WHERE activation_receipt_sha=?",
                (activation["activation_receipt_sha"],),
            ).fetchone()
        )

    def test_crash_before_and_after_activation_recovers_idempotently(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        _, pair_receipt, direction_check = self._activation_evidence(staged)
        broken = dict(direction_check, evidence_sha="f" * 64)
        before = self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
        with self.assertRaises(ValueError):
            audit.activate_staged_candidate(
                self.conn,
                snapshot=snapshot,
                staged_candidate=staged["candidates"][0],
                pair_receipt=pair_receipt,
                direction_check=broken,
            )
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0], before)
        crash_type = getattr(audit_module, "ActivationCrash", _ZeroAudit.ActivationCrash)
        with self.assertRaises(crash_type):
            audit.activate_staged_candidate(
                self.conn,
                snapshot=snapshot,
                staged_candidate=staged["candidates"][0],
                pair_receipt=pair_receipt,
                direction_check=direction_check,
                fault_after_commit=True,
            )
        replay = audit.activate_staged_candidate(
            self.conn,
            snapshot=snapshot,
            staged_candidate=staged["candidates"][0],
            pair_receipt=pair_receipt,
            direction_check=direction_check,
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0], before + 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM audit_activation_maps").fetchone()[0], 1)

    def test_status_derivation_is_priority_ordered(self):
        base = dict(
            identity_valid=True,
            verified_hits=[],
            coverage_complete=True,
            adjudication_complete=True,
            semantic_policy_qualified=True,
            unresolved_conflict=False,
            exhausted_reason=None,
            no_match_basis="l1_calibrated",
        )
        cases = (
            ({**base, "identity_valid": False, "verified_hits": ["x"], "coverage_complete": False}, ("invalid", "invalid_identity")),
            ({**base, "verified_hits": ["x"], "coverage_complete": False, "exhausted_reason": "budget_exceeded"}, ("overlap_found", "match_found_partial_coverage")),
            ({**base, "coverage_complete": False, "unresolved_conflict": True, "exhausted_reason": "budget_exceeded"}, ("partial", "budget_exceeded")),
            ({**base, "unresolved_conflict": True}, ("uncertain", "conflict")),
            ({**base, "semantic_policy_qualified": False, "no_match_basis": None}, ("uncertain", "semantic_policy_unqualified")),
            (base, ("complete_no_match", "complete_no_match")),
        )
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(audit.derive_final_status(**kwargs), expected)

    def test_verified_hit_with_missing_leaf_is_overlap_partial_coverage(self):
        status = audit.derive_final_status(
            identity_valid=True,
            verified_hits=["blocking-1"],
            coverage_complete=False,
            adjudication_complete=False,
            semantic_policy_qualified=False,
            unresolved_conflict=False,
            exhausted_reason="missing_leaf",
            no_match_basis=None,
        )
        self.assertEqual(status, ("overlap_found", "match_found_partial_coverage"))

    def test_clean_coverage_without_qualification_is_uncertain(self):
        status = audit.derive_final_status(
            identity_valid=True,
            verified_hits=[],
            coverage_complete=True,
            adjudication_complete=True,
            semantic_policy_qualified=False,
            unresolved_conflict=False,
            exhausted_reason=None,
            no_match_basis=None,
        )
        self.assertEqual(status, ("uncertain", "semantic_policy_unqualified"))

    def test_complete_no_match_requires_basis_and_all_three_gates(self):
        base = dict(
            identity_valid=True,
            verified_hits=[],
            coverage_complete=True,
            adjudication_complete=True,
            semantic_policy_qualified=True,
            unresolved_conflict=False,
            exhausted_reason=None,
            no_match_basis="l1_calibrated",
        )
        self.assertEqual(audit.derive_final_status(**base)[0], "complete_no_match")
        for field, value in (
            ("coverage_complete", False),
            ("adjudication_complete", False),
            ("semantic_policy_qualified", False),
            ("no_match_basis", None),
        ):
            with self.subTest(field=field):
                changed = dict(base, **{field: value})
                self.assertNotEqual(audit.derive_final_status(**changed)[0], "complete_no_match")

    def test_closed_receipt_rejects_basis_and_excluded_batch_ids_hash_aliases(self):
        snapshot = self._snapshot()
        retrieval, adjudication, qualification = self._receipt_inputs(snapshot)
        for alias in ("basis", "excluded_batch_ids_hash"):
            with self.subTest(alias=alias):
                invalid = dict(retrieval, **{alias: "alias-value"})
                with self.assertRaises(ValueError):
                    audit.build_l1_receipt(snapshot, invalid, adjudication, qualification)

    def test_l1_receipt_rejects_semantic_only_hits_and_false_coverage(self):
        snapshot = self._snapshot()
        retrieval, adjudication, qualification = self._receipt_inputs(snapshot)
        semantic_hit = dict(
            adjudication,
            verified_hits=[
                {
                    "lineage_id": "lineage-semantic",
                    "source": "semantic_rank",
                    "semantic_relation": "blocking_duplicate",
                }
            ],
        )
        with self.assertRaises(ValueError):
            audit.build_l1_receipt(
                snapshot, retrieval, semantic_hit, qualification
            )
        false_coverage = dict(retrieval, missing_ids=["missing-lineage"])
        with self.assertRaises(ValueError):
            audit.build_l1_receipt(
                snapshot, false_coverage, adjudication, qualification
            )

    def test_snapshot_exclusion_sha_binds_the_published_policy(self):
        snapshot = self._snapshot()
        policy = json.loads(
            (ROOT / "history/history-audit-policy-v2.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            snapshot["exclusion_policy_sha"],
            contract.framed_sha256(
                "history-exclusion-policy-v2",
                contract.canonical_bytes(policy["exclusion_policy"]),
            ),
        )

    def test_identical_query_views_vote_once(self):
        rankings = {
            "fts": [
                {"candidate_id": "candidate-a", "query_view_id": "story", "score": 1.0},
                {"candidate_id": "candidate-a", "query_view_id": "story", "score": 1.0},
                {"candidate_id": "candidate-a", "query_view_id": "title", "score": 0.5},
            ]
        }
        fused = audit.fair_family_fusion(rankings, {"candidate-a": "lineage-a"})
        self.assertEqual(len(fused["lineages"]), 1)
        self.assertEqual(fused["lineages"][0]["family_scores"], {"fts": 1.0})
        self.assertEqual(fused["lineages"][0]["score"], 1.0)

    def test_multiple_revisions_vote_once_per_lineage_and_family(self):
        rankings = {
            "fts": [
                {"candidate_id": "revision-a1", "query_view_id": "story", "score": 0.4},
                {"candidate_id": "revision-a2", "query_view_id": "story", "score": 0.8},
            ],
            "hash_dense": [
                {"candidate_id": "revision-a1", "query_view_id": "story", "score": 0.5},
                {"candidate_id": "revision-a2", "query_view_id": "story", "score": 0.7},
            ],
        }
        lineage = {"revision-a1": "lineage-a", "revision-a2": "lineage-a"}
        fused = audit.fair_family_fusion(rankings, lineage)
        self.assertEqual(len(fused["lineages"]), 1)
        self.assertEqual(
            fused["lineages"][0]["family_scores"],
            {"fts": 0.8, "hash_dense": 0.7},
        )
        self.assertEqual(
            fused["lineages"][0]["candidate_ids"],
            ["revision-a1", "revision-a2"],
        )

    def test_mandatory_candidates_bypass_routine_cutoff_but_semantic_hits_do_not(self):
        rankings = {
            "exact": [
                {"candidate_id": "exact-a", "query_view_id": "story", "score": 0.1}
            ],
            "fts": [
                {"candidate_id": "lexical-b", "query_view_id": "story", "score": 1.0}
            ],
            "semantic": [
                {"candidate_id": "semantic-c", "query_view_id": "story", "score": 0.9}
            ],
        }
        fused = audit.fair_family_fusion(
            rankings,
            {"exact-a": "lineage-a", "lexical-b": "lineage-b", "semantic-c": "lineage-c"},
        )
        selected = audit.select_l1_comparisons(fused, routine_cutoff=1)
        self.assertEqual(selected["mandatory_lineage_ids"], ["lineage-a"])
        self.assertEqual(selected["routine_lineage_ids"], ["lineage-b"])
        self.assertEqual(selected["selected_lineage_ids"], ["lineage-a", "lineage-b"])
        self.assertNotIn("lineage-c", selected["selected_lineage_ids"])

    def test_missing_mandatory_comparison_is_partial_without_hit(self):
        fused = audit.fair_family_fusion(
            {
                "declared_parent": [
                    {"candidate_id": "parent-a", "query_view_id": "parent", "score": 0.1}
                ]
            },
            {"parent-a": "lineage-a"},
        )
        selected = audit.select_l1_comparisons(fused, routine_cutoff=0)
        coverage = audit.evaluate_l1_coverage(selected, [])
        self.assertFalse(coverage["coverage_complete"])
        self.assertEqual(coverage["missing_mandatory_lineage_ids"], ["lineage-a"])
        self.assertEqual(
            audit.derive_final_status(
                identity_valid=True,
                verified_hits=[],
                coverage_complete=coverage["coverage_complete"],
                adjudication_complete=False,
                semantic_policy_qualified=False,
                unresolved_conflict=False,
                exhausted_reason=coverage["exhausted_reason"],
                no_match_basis=None,
            ),
            ("partial", "missing_mandatory_comparison"),
        )

    def test_verified_mandatory_hit_survives_missing_semantic_work(self):
        status = audit.derive_final_status(
            identity_valid=True,
            verified_hits=[{"lineage_id": "lineage-a", "source": "exact"}],
            coverage_complete=False,
            adjudication_complete=False,
            semantic_policy_qualified=False,
            unresolved_conflict=False,
            exhausted_reason="semantic_work_incomplete",
            no_match_basis=None,
        )
        self.assertEqual(status, ("overlap_found", "match_found_partial_coverage"))

    def test_flat_family_results_are_stable_without_metadata(self):
        flat = {
            "exact": [
                {"candidate_id": "candidate-a", "query_view_id": "story", "score": 1.0}
            ],
            "fts": [
                {"candidate_id": "candidate-b", "query_view_id": "story", "score": 0.6}
            ],
            "hash_dense": [
                {"candidate_id": "candidate-b", "query_view_id": "story", "score": 0.5}
            ],
        }
        lineage = {"candidate-a": "lineage-a", "candidate-b": "lineage-b"}
        without_metadata = audit.fair_family_fusion(flat, lineage)
        with_empty_metadata = audit.fair_family_fusion(
            {**flat, "metadata": []}, lineage
        )
        self.assertEqual(without_metadata["lineages"], with_empty_metadata["lineages"])
        self.assertEqual(
            without_metadata["family_semantics"]["hash_dense"],
            "lexical_approximation",
        )
        self.assertEqual(
            audit.select_l1_comparisons(without_metadata, routine_cutoff=2),
            audit.select_l1_comparisons(with_empty_metadata, routine_cutoff=2),
        )

    def test_l1_projection_reads_apply_the_frozen_watermark(self):
        policy = projection.load_policy(ROOT / "history/retrieval-policy-v1.json")
        projection.rebuild(self.conn, policy)
        snapshot = self._snapshot()
        appended = history_store.append_rows(
            self.conn,
            [row("post watermark exact story")],
            {"run_id": "post-watermark"},
        )
        projection.rebuild(self.conn, policy)
        post_id = appended["candidate_ids"][0]
        rankings = audit.read_l1_rankings(
            self.conn, snapshot, "post watermark exact story", depth=10
        )
        returned = {
            item["candidate_id"]
            for family in rankings.values()
            for item in family
        }
        self.assertNotIn(post_id, returned)
        self.assertTrue(returned.issubset(set(snapshot["expected_asset_ids"])))


if __name__ == "__main__":
    unittest.main()
