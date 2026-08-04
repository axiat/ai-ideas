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
                               semantic_relation=None, lineage_relation=None,
                               evidence_sha=None, direction_fit=None,
                               direction_evidence=None):
        return {
            "staging_candidate_id": staged_candidate["staging_candidate_id"],
            "direction_fit": direction_fit,
            "direction_evidence": direction_evidence,
            "verdict_sha256": SHA,
        }

    @staticmethod
    def record_batch_direction_gate(
        conn, *, staged_batch, direction_receipt, verdict_tsv
    ):
        return {
            "gate_sha256": SHA,
            "candidate_mapping": [],
            "verdicts": [],
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
        pair_plan, pair_receipt = self._pair_evidence(staged)
        direction_checks = self._direction_verdicts(staged)
        return pair_plan, pair_receipt, direction_checks[0]

    def _pair_evidence(self, staged):
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
        return pair_plan, pair_receipt

    def _direction_verdicts(self, staged, fits=None):
        fits = fits or ["in-scope"] * len(staged["candidates"])
        gate = audit.record_batch_direction_gate(
            self.conn,
            staged_batch=staged,
            direction_receipt=self.direction,
            verdict_tsv=self._direction_tsv(staged, fits=fits),
        )
        return gate["verdicts"]

    def _direction_tsv(self, staged, fits=None, evidences=None):
        ordered = sorted(
            staged["candidates"], key=lambda item: item["source_order"]
        )
        fits = fits or ["in-scope"] * len(ordered)
        evidences = evidences or [
            f"selector evidence {index + 1}" for index in range(len(ordered))
        ]
        rows = ["id\tdirection-fit\tdirection-evidence"]
        rows.extend(
            f"I{index + 1}\t{fit}\t{evidence}"
            for index, (fit, evidence) in enumerate(zip(fits, evidences))
        )
        return ("\n".join(rows) + "\n").encode("utf-8")

    def _direct_activation_insert(self, staged_candidate, pair_receipt):
        appended = history_store.append_rows(
            self.conn,
            [staged_candidate["raw_candidate"]],
            {"run_id": "direct-direction-gate"},
        )
        candidate = self.conn.execute(
            "SELECT candidate_id,source_sequence FROM candidates "
            "WHERE candidate_id=?",
            (appended["candidate_ids"][0],),
        ).fetchone()
        activation_sha = hashlib.sha256(
            ("direct:" + staged_candidate["staging_candidate_id"]).encode()
        ).hexdigest()
        self.conn.execute(
            """
            INSERT INTO audit_activation_receipts(
              activation_receipt_sha, staging_candidate_id,
              receipt_json, created_at
            ) VALUES(?, ?, '{}', '2026-08-03T00:00:00Z')
            """,
            (activation_sha, staged_candidate["staging_candidate_id"]),
        )
        self.conn.execute(
            """
            INSERT INTO audit_activation_maps(
              staging_candidate_id, legacy_candidate_id, source_sequence,
              raw_artifact_sha, pair_plan_sha, pair_result_sha,
              activation_receipt_sha, activated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
            """,
            (
                staged_candidate["staging_candidate_id"],
                candidate["candidate_id"], candidate["source_sequence"],
                staged_candidate["raw_artifact_sha"],
                pair_receipt["pair_plan_sha"],
                pair_receipt["pair_result_sha"], activation_sha,
            ),
        )

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

    def _inject_same_batch_staging(
        self, *, story="injected nonmember", staging_id=None
    ):
        staging_id = staging_id or "stg-v2-" + "e" * 64
        raw_candidate = history_store._normalize_append_row(row(story))
        raw_artifact_sha = hashlib.sha256(raw_candidate).hexdigest()
        candidate_hash = contract.framed_sha256(
            "history-candidate-content-v2", raw_candidate
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            audit_store.insert_authorized_batch_staging(
                self.conn,
                staging_candidate_id=staging_id,
                run_id=self.run_id,
                batch_id=self.batch_id,
                candidate_hash=candidate_hash,
                raw_artifact_sha=raw_artifact_sha,
                source_order=99,
                created_at="2026-08-03T00:00:00Z",
            )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return {
            "staging_candidate_id": staging_id,
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "source_order": 99,
            "candidate_hash": candidate_hash,
            "raw_artifact_sha": raw_artifact_sha,
            "raw_candidate": raw_candidate,
        }

    def _install_mixed_strict_pair(self, snapshot, staged):
        nonmember = self._inject_same_batch_staging(
            story="legacy pair nonmember", staging_id="legacy-nonmember"
        )
        pair_plan = audit.plan_batch_pairs(staged)
        pair_result_sha = "d" * 64
        pair_receipt = {
            "run_id": self.run_id,
            "batch_id": self.batch_id,
            "snapshot_id": snapshot["snapshot_id"],
            "pair_plan_sha": pair_plan["pair_plan_sha"],
            "pair_result_sha": pair_result_sha,
            "pair_count": 1,
        }
        self.conn.execute(
            """
            INSERT INTO audit_batch_pair_receipts(
              run_id, batch_id, snapshot_id, pair_plan_sha, pair_result_sha,
              pair_count, completed_at
            ) VALUES(?, ?, ?, ?, ?, 1, '2026-08-03T00:00:00Z')
            """,
            (
                self.run_id, self.batch_id, snapshot["snapshot_id"],
                pair_plan["pair_plan_sha"], pair_result_sha,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO audit_batch_pair_set_bindings(
              run_id, batch_id, snapshot_id, pair_plan_sha, pair_result_sha,
              current_batch_ids_hash, member_count, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, 2, '2026-08-03T00:00:00Z')
            """,
            (
                self.run_id, self.batch_id, snapshot["snapshot_id"],
                pair_plan["pair_plan_sha"], pair_result_sha,
                snapshot["current_batch_ids_hash"],
            ),
        )
        self.conn.execute(
            """
            INSERT INTO audit_batch_pairs(
              run_id, batch_id, left_staging_candidate_id,
              right_staging_candidate_id, pair_plan_sha, pair_result_sha,
              created_at
            ) VALUES(?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
            """,
            (
                self.run_id, self.batch_id, nonmember["staging_candidate_id"],
                staged["candidates"][1]["staging_candidate_id"],
                pair_plan["pair_plan_sha"], pair_result_sha,
            ),
        )
        return pair_receipt

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

    def test_new_staging_rows_require_host_authority_for_any_id_shape(self):
        inserted = []
        for index, staging_id in enumerate((
            "new-nonprefix-candidate",
            "stg-v2-" + "f" * 64,
        )):
            try:
                self.conn.execute(
                    """
                    INSERT INTO audit_batch_staging(
                      staging_candidate_id, run_id, batch_id, candidate_hash,
                      raw_artifact_sha, source_order, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
                    """,
                    (
                        staging_id, self.run_id, self.batch_id,
                        hashlib.sha256(f"candidate-{index}".encode()).hexdigest(),
                        hashlib.sha256(f"raw-{index}".encode()).hexdigest(),
                        index,
                    ),
                )
            except sqlite3.IntegrityError:
                inserted.append(False)
            else:
                inserted.append(True)
        self.conn.rollback()
        self.assertEqual(inserted, [False, False])

    def test_active_staging_direction_triggers_use_authority_kind_not_prefix(self):
        names = (
            "audit_batch_pairs_owner_and_order_guard",
            "audit_batch_pairs_set_binding_guard",
            "audit_activation_maps_evidence_guard",
            "audit_activation_maps_batch_direction_guard",
            "audit_direction_checks_staging_owner_guard",
        )
        rows = self.conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            f"AND name IN ({','.join('?' for _ in names)})",
            names,
        ).fetchall()
        self.assertEqual({row["name"] for row in rows}, set(names))
        for row in rows:
            self.assertNotIn("GLOB", row["sql"])
            self.assertIn("authority_kind", row["sql"])

    def test_staging_authority_sidecars_are_immutable_and_reopen_exactly(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        before = self.conn.execute(
            "SELECT * FROM audit_batch_staging_authorities_v2 ORDER BY source_order"
        ).fetchall()
        self.assertEqual(
            [row["authority_kind"] for row in before],
            ["host_issued", "host_issued"],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE audit_batch_staging_authorities_v2 "
                "SET authority_kind='migration_v2' WHERE source_order=0"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM audit_batch_staging_authorities_v2 WHERE source_order=0"
            )
        self.conn.close()
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        audit_store.init_schema(self.conn)
        replay = self._staged(snapshot=snapshot)
        after = self.conn.execute(
            "SELECT * FROM audit_batch_staging_authorities_v2 ORDER BY source_order"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in after], [tuple(row) for row in before]
        )
        self.assertEqual(
            [item["staging_candidate_id"] for item in replay["candidates"]],
            [item["staging_candidate_id"] for item in staged["candidates"]],
        )

    def test_individual_v2_direction_verdict_requires_batch_gate(self):
        staged = self._staged()
        with self.assertRaisesRegex(ValueError, "direction_gate_authority_required"):
            audit.record_direction_check(
                self.conn,
                staged_candidate=staged["candidates"][0],
                direction_receipt=self.direction,
                direction_fit="in-scope",
                direction_evidence="caller verdict",
            )

    def test_batch_direction_gate_maps_selector_ids_by_source_order(self):
        ids = (
            "stg-v2-" + "f" * 64,
            "stg-v2-" + "0" * 64,
        )
        snapshot = self._snapshot(ids=ids)
        staged = self._staged(snapshot=snapshot, ids=ids)
        gate = audit.record_batch_direction_gate(
            self.conn,
            staged_batch=staged,
            direction_receipt=self.direction,
            verdict_tsv=self._direction_tsv(staged),
        )
        expected = [
            {
                "selector_id": "I1",
                "staging_candidate_id": ids[0],
                "source_order": 0,
            },
            {
                "selector_id": "I2",
                "staging_candidate_id": ids[1],
                "source_order": 1,
            },
        ]
        self.assertEqual(gate["candidate_mapping"], expected)
        self.assertEqual(
            [item["staging_candidate_id"] for item in gate["verdicts"]],
            list(ids),
        )

    def test_batch_direction_gate_rejects_caller_source_order_drift(self):
        staged = self._staged()
        tampered = dict(
            staged,
            candidates=[dict(item) for item in staged["candidates"]],
        )
        tampered["candidates"][0]["source_order"] = 99
        with self.assertRaisesRegex(ValueError, "identity does not replay"):
            audit.record_batch_direction_gate(
                self.conn,
                staged_batch=tampered,
                direction_receipt=self.direction,
                verdict_tsv=self._direction_tsv(staged),
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_batch_direction_gates_v2"
            ).fetchone()[0],
            0,
        )

    def test_batch_direction_gate_half_write_rolls_back_and_then_commits(self):
        staged = self._staged()
        self.conn.execute(
            """
            CREATE TEMP TRIGGER fail_second_direction_binding
            BEFORE INSERT ON main.audit_batch_direction_gate_bindings_v2
            WHEN NEW.source_order=1
            BEGIN
              SELECT RAISE(ABORT,'fault injected at second direction binding');
            END
            """
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "fault injected at second direction binding"
        ):
            audit.record_batch_direction_gate(
                self.conn,
                staged_batch=staged,
                direction_receipt=self.direction,
                verdict_tsv=self._direction_tsv(staged),
            )
        self.assertFalse(self.conn.in_transaction)
        for table in (
            "audit_batch_direction_gates_v2",
            "audit_batch_direction_gate_bindings_v2",
            "audit_batch_direction_verdicts_v2",
        ):
            self.assertEqual(
                self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                0,
            )
        self.conn.execute("DROP TRIGGER temp.fail_second_direction_binding")
        gate = audit.record_batch_direction_gate(
            self.conn,
            staged_batch=staged,
            direction_receipt=self.direction,
            verdict_tsv=self._direction_tsv(staged),
        )
        self.assertEqual(gate["member_count"], 2)

    def test_batch_direction_gate_reopens_and_exactly_replays(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        verdict_tsv = self._direction_tsv(staged)
        first = audit.record_batch_direction_gate(
            self.conn,
            staged_batch=staged,
            direction_receipt=self.direction,
            verdict_tsv=verdict_tsv,
        )
        self.conn.close()
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        audit_store.init_schema(self.conn)
        replay = audit.record_batch_direction_gate(
            self.conn,
            staged_batch=staged,
            direction_receipt=self.direction,
            verdict_tsv=verdict_tsv,
        )
        self.assertEqual(replay, first)
        with self.assertRaisesRegex(ValueError, "conflicts with durable state"):
            audit.record_batch_direction_gate(
                self.conn,
                staged_batch=staged,
                direction_receipt=self.direction,
                verdict_tsv=self._direction_tsv(
                    staged, evidences=["changed evidence", "selector evidence 2"]
                ),
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
        self.conn.execute(
            "DROP TRIGGER IF EXISTS audit_batch_pair_receipts_snapshot_set_guard"
        )
        self.conn.execute(
            "DROP TRIGGER IF EXISTS audit_batch_pair_set_bindings_identity_guard"
        )
        self.conn.execute(
            "DROP TRIGGER IF EXISTS audit_batch_pairs_set_binding_guard"
        )
        pair_receipt = self._insert_foreign_pair_receipt(staged, foreign)
        foreign_set = self.conn.execute(
            "SELECT * FROM audit_snapshot_batch_sets WHERE snapshot_id=?",
            (foreign["snapshot_id"],),
        ).fetchone()
        self.conn.execute(
            """
            INSERT INTO audit_batch_pair_set_bindings(
              run_id, batch_id, snapshot_id, pair_plan_sha, pair_result_sha,
              current_batch_ids_hash, member_count, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
            """,
            (
                staged["run_id"], staged["batch_id"], foreign["snapshot_id"],
                pair_receipt["pair_plan_sha"], pair_receipt["pair_result_sha"],
                foreign_set["current_batch_ids_hash"], foreign_set["member_count"],
            ),
        )
        direction_check = {}
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
                if migration.component not in {
                    "l1-pair-snapshot-ownership",
                    "l1-snapshot-batch-membership",
                    "l1-strict-pair-completion",
                    "l1-batch-direction-authority",
                    "batch-staging-authority",
                    "batch-direction-gate-authority",
                    "metadata-direction-gate-provenance",
                }
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

    def test_membership_upgrade_probe_rejects_strict_snapshot_without_set(self):
        conn = history_store.connect(self.root / "membership-upgrade.sqlite3")
        try:
            history_store.init_schema(conn)
            old_migrations = tuple(
                migration
                for migration in audit_store.MIGRATIONS
                if migration.component not in {
                    "l1-snapshot-batch-membership",
                    "l1-strict-pair-completion",
                    "l1-batch-direction-authority",
                    "batch-staging-authority",
                    "batch-direction-gate-authority",
                    "metadata-direction-gate-provenance",
                }
            )
            with mock.patch.object(audit_store, "MIGRATIONS", old_migrations):
                audit_store.init_schema(conn)
            conn.execute(
                """
                INSERT INTO audit_run_manifests(
                  run_id, manifest_schema_version, plan_hash,
                  manifest_json, created_at
                ) VALUES('run-membership', 'history-audit-manifest-v2', ?, '{}',
                         '2026-08-03T00:00:00Z')
                """,
                ("1" * 64,),
            )
            member_id = "stg-v2-" + "2" * 64
            member_hash = contract.ordered_set_sha256(
                "history-current-batch-ids-v2", [member_id]
            )
            conn.execute(
                """
                INSERT INTO audit_snapshots(
                  snapshot_id, snapshot_hash, history_as_of_watermark,
                  current_batch_id_namespace, current_batch_ids_hash,
                  exclusion_policy_sha, expected_asset_ids_hash, created_at,
                  run_id, batch_id
                ) VALUES(?, ?, 0, 'history-v2-staging-v1', ?, ?, ?,
                         '2026-08-03T00:00:00Z', 'run-membership',
                         'batch-membership')
                """,
                ("3" * 64, "4" * 64, member_hash, "5" * 64, "6" * 64),
            )
            with self.assertRaises(audit_store.AuditMigrationError):
                audit_store.init_schema(conn)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM audit_schema_migrations "
                    "WHERE component='l1-snapshot-batch-membership'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_activation_rejects_same_batch_staging_outside_frozen_member_set(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        _, pair_receipt, _ = self._activation_evidence(staged)
        injected = self._inject_same_batch_staging()
        direction_check = {}
        before = self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
        with self.assertRaises(ValueError):
            audit.activate_staged_candidate(
                self.conn,
                snapshot=snapshot,
                staged_candidate=injected,
                pair_receipt=pair_receipt,
                direction_check=direction_check,
            )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
            before,
        )

    def test_database_rejects_strict_activation_for_snapshot_nonmember(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        _, pair_receipt, _ = self._activation_evidence(staged)
        injected = self._inject_same_batch_staging(story="direct injected nonmember")
        appended = history_store.append_rows(
            self.conn,
            [injected["raw_candidate"]],
            {"run_id": "direct-injected"},
        )
        candidate = self.conn.execute(
            "SELECT candidate_id, source_sequence FROM candidates WHERE candidate_id=?",
            (appended["candidate_ids"][0],),
        ).fetchone()
        activation_sha = "a" * 64
        self.conn.execute(
            """
            INSERT INTO audit_activation_receipts(
              activation_receipt_sha, staging_candidate_id,
              receipt_json, created_at
            ) VALUES(?, ?, '{}', '2026-08-03T00:00:00Z')
            """,
            (activation_sha, injected["staging_candidate_id"]),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO audit_activation_maps(
                  staging_candidate_id, legacy_candidate_id, source_sequence,
                  raw_artifact_sha, pair_plan_sha, pair_result_sha,
                  activation_receipt_sha, activated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
                """,
                (
                    injected["staging_candidate_id"], candidate["candidate_id"],
                    candidate["source_sequence"], injected["raw_artifact_sha"],
                    pair_receipt["pair_plan_sha"],
                    pair_receipt["pair_result_sha"], activation_sha,
                ),
            )

    def test_activation_rejects_mixed_pair_that_leaves_frozen_member_unpaired(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        self.conn.execute(
            "DROP TRIGGER audit_batch_pairs_set_binding_guard"
        )
        pair_receipt = self._install_mixed_strict_pair(snapshot, staged)
        selected = staged["candidates"][1]
        direction_check = {}
        before = self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
        with self.assertRaises(ValueError):
            audit.activate_staged_candidate(
                self.conn,
                snapshot=snapshot,
                staged_candidate=selected,
                pair_receipt=pair_receipt,
                direction_check=direction_check,
            )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
            before,
        )

    def test_database_rejects_member_activation_over_mixed_strict_pair(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        self.conn.execute(
            "DROP TRIGGER audit_batch_pairs_set_binding_guard"
        )
        pair_receipt = self._install_mixed_strict_pair(snapshot, staged)
        selected = staged["candidates"][1]
        appended = history_store.append_rows(
            self.conn, [selected["raw_candidate"]], {"run_id": "mixed-direct"}
        )
        candidate = self.conn.execute(
            "SELECT candidate_id, source_sequence FROM candidates WHERE candidate_id=?",
            (appended["candidate_ids"][0],),
        ).fetchone()
        activation_sha = "a" * 64
        self.conn.execute(
            """
            INSERT INTO audit_activation_receipts(
              activation_receipt_sha, staging_candidate_id,
              receipt_json, created_at
            ) VALUES(?, ?, '{}', '2026-08-03T00:00:00Z')
            """,
            (activation_sha, selected["staging_candidate_id"]),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO audit_activation_maps(
                  staging_candidate_id, legacy_candidate_id, source_sequence,
                  raw_artifact_sha, pair_plan_sha, pair_result_sha,
                  activation_receipt_sha, activated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
                """,
                (
                    selected["staging_candidate_id"], candidate["candidate_id"],
                    candidate["source_sequence"], selected["raw_artifact_sha"],
                    pair_receipt["pair_plan_sha"],
                    pair_receipt["pair_result_sha"], activation_sha,
                ),
            )

    def test_database_rejects_nonmember_endpoint_under_strict_pair_binding(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        with self.assertRaises(sqlite3.IntegrityError):
            self._install_mixed_strict_pair(snapshot, staged)

    def test_database_does_not_route_strict_binding_through_legacy_activation(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        self.conn.execute("DROP TRIGGER audit_batch_pairs_set_binding_guard")
        pair_receipt = self._install_mixed_strict_pair(snapshot, staged)
        nonmember = self.conn.execute(
            "SELECT * FROM audit_batch_staging WHERE staging_candidate_id=?",
            ("legacy-nonmember",),
        ).fetchone()
        raw_candidate = history_store._normalize_append_row(
            row("legacy pair nonmember")
        )
        appended = history_store.append_rows(
            self.conn, [raw_candidate], {"run_id": "strict-legacy-direct"}
        )
        candidate = self.conn.execute(
            "SELECT candidate_id, source_sequence FROM candidates WHERE candidate_id=?",
            (appended["candidate_ids"][0],),
        ).fetchone()
        activation_sha = "b" * 64
        self.conn.execute(
            """
            INSERT INTO audit_activation_receipts(
              activation_receipt_sha, staging_candidate_id,
              receipt_json, created_at
            ) VALUES(?, 'legacy-nonmember', '{}', '2026-08-03T00:00:00Z')
            """,
            (activation_sha,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO audit_activation_maps(
                  staging_candidate_id, legacy_candidate_id, source_sequence,
                  raw_artifact_sha, pair_plan_sha, pair_result_sha,
                  activation_receipt_sha, activated_at
                ) VALUES('legacy-nonmember', ?, ?, ?, ?, ?, ?,
                         '2026-08-03T00:00:00Z')
                """,
                (
                    candidate["candidate_id"], candidate["source_sequence"],
                    nonmember["raw_artifact_sha"], pair_receipt["pair_plan_sha"],
                    pair_receipt["pair_result_sha"], activation_sha,
                ),
            )

    def test_strict_pair_completion_upgrade_probe_rejects_mixed_pair(self):
        conn = history_store.connect(self.root / "pair-completion-upgrade.sqlite3")
        try:
            history_store.init_schema(conn)
            history_store.import_tsv_epoch(conn, self.ledger)
            old_migrations = tuple(
                migration
                for migration in audit_store.MIGRATIONS
                if migration.component not in {
                    "l1-strict-pair-completion",
                    "l1-batch-direction-authority",
                    "batch-staging-authority",
                    "batch-direction-gate-authority",
                    "metadata-direction-gate-provenance",
                }
            )
            with mock.patch.object(audit_store, "MIGRATIONS", old_migrations):
                audit_store.init_schema(conn)
            conn.execute(
                """
                INSERT INTO audit_run_manifests(
                  run_id, manifest_schema_version, plan_hash,
                  manifest_json, created_at
                ) VALUES('run-upgrade-pair', 'history-audit-manifest-v2', ?, '{}',
                         '2026-08-03T00:00:00Z')
                """,
                ("1" * 64,),
            )
            member_ids = ["stg-v2-" + "2" * 64, "stg-v2-" + "3" * 64]
            snapshot = audit.freeze_snapshot(
                conn,
                run_id="run-upgrade-pair",
                batch_id="batch-upgrade-pair",
                current_batch_ids=member_ids,
            )
            staged = audit.stage_raw_batch(
                conn,
                snapshot=snapshot,
                raw_candidates=[
                    {"staging_candidate_id": member_ids[0], "raw_candidate": row("a")},
                    {"staging_candidate_id": member_ids[1], "raw_candidate": row("b")},
                ],
                direction_receipt=self.direction,
            )
            nonmember_raw = history_store._normalize_append_row(row("legacy"))
            conn.execute(
                """
                INSERT INTO audit_batch_staging(
                  staging_candidate_id, run_id, batch_id, candidate_hash,
                  raw_artifact_sha, source_order, created_at
                ) VALUES('legacy-upgrade', 'run-upgrade-pair',
                         'batch-upgrade-pair', ?, ?, 99,
                         '2026-08-03T00:00:00Z')
                """,
                (
                    contract.framed_sha256(
                        "history-candidate-content-v2", nonmember_raw
                    ),
                    hashlib.sha256(nonmember_raw).hexdigest(),
                ),
            )
            pair_plan = audit.plan_batch_pairs(staged)
            result_sha = "d" * 64
            conn.execute(
                """
                INSERT INTO audit_batch_pair_receipts(
                  run_id, batch_id, snapshot_id, pair_plan_sha,
                  pair_result_sha, pair_count, completed_at
                ) VALUES('run-upgrade-pair', 'batch-upgrade-pair', ?, ?, ?, 1,
                         '2026-08-03T00:00:00Z')
                """,
                (snapshot["snapshot_id"], pair_plan["pair_plan_sha"], result_sha),
            )
            conn.execute(
                """
                INSERT INTO audit_batch_pair_set_bindings(
                  run_id, batch_id, snapshot_id, pair_plan_sha, pair_result_sha,
                  current_batch_ids_hash, member_count, created_at
                ) VALUES('run-upgrade-pair', 'batch-upgrade-pair', ?, ?, ?, ?, 2,
                         '2026-08-03T00:00:00Z')
                """,
                (
                    snapshot["snapshot_id"], pair_plan["pair_plan_sha"],
                    result_sha, snapshot["current_batch_ids_hash"],
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_batch_pairs(
                  run_id, batch_id, left_staging_candidate_id,
                  right_staging_candidate_id, pair_plan_sha, pair_result_sha,
                  created_at
                ) VALUES('run-upgrade-pair', 'batch-upgrade-pair',
                         'legacy-upgrade', ?, ?, ?, '2026-08-03T00:00:00Z')
                """,
                (member_ids[1], pair_plan["pair_plan_sha"], result_sha),
            )
            appended = history_store.append_rows(
                conn, [row("b")], {"run_id": "upgrade-mixed-activation"}
            )
            candidate = conn.execute(
                "SELECT candidate_id, source_sequence FROM candidates "
                "WHERE candidate_id=?",
                (appended["candidate_ids"][0],),
            ).fetchone()
            activation_sha = "e" * 64
            selected = staged["candidates"][1]
            conn.execute(
                """
                INSERT INTO audit_activation_receipts(
                  activation_receipt_sha, staging_candidate_id,
                  receipt_json, created_at
                ) VALUES(?, ?, '{}', '2026-08-03T00:00:00Z')
                """,
                (activation_sha, selected["staging_candidate_id"]),
            )
            conn.execute(
                """
                INSERT INTO audit_activation_maps(
                  staging_candidate_id, legacy_candidate_id, source_sequence,
                  raw_artifact_sha, pair_plan_sha, pair_result_sha,
                  activation_receipt_sha, activated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
                """,
                (
                    selected["staging_candidate_id"], candidate["candidate_id"],
                    candidate["source_sequence"], selected["raw_artifact_sha"],
                    pair_plan["pair_plan_sha"], result_sha, activation_sha,
                ),
            )
            with self.assertRaises(audit_store.AuditMigrationError):
                audit_store.init_schema(conn)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM audit_schema_migrations "
                    "WHERE component='l1-strict-pair-completion'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_direction_authority_upgrade_rejects_preexisting_unissued_activation(self):
        self.assertTrue(any(
            migration.component == "l1-batch-direction-authority"
            for migration in audit_store.MIGRATIONS
        ))
        conn = history_store.connect(self.root / "direction-upgrade.sqlite3")
        try:
            history_store.init_schema(conn)
            history_store.import_tsv_epoch(conn, self.ledger)
            old_migrations = tuple(
                migration
                for migration in audit_store.MIGRATIONS
                if migration.component not in {
                    "l1-batch-direction-authority",
                    "batch-direction-gate-authority",
                    "metadata-direction-gate-provenance",
                }
            )
            with mock.patch.object(audit_store, "MIGRATIONS", old_migrations):
                audit_store.init_schema(conn)
            conn.execute(
                """
                INSERT INTO audit_run_manifests(
                  run_id, manifest_schema_version, plan_hash,
                  manifest_json, created_at
                ) VALUES('run-direction-upgrade', 'history-audit-manifest-v2',
                         ?, '{}', '2026-08-03T00:00:00Z')
                """,
                ("1" * 64,),
            )
            staging_id = "stg-v2-" + "7" * 64
            snapshot = audit.freeze_snapshot(
                conn,
                run_id="run-direction-upgrade",
                batch_id="batch-direction-upgrade",
                current_batch_ids=[staging_id],
            )
            staged = audit.stage_raw_batch(
                conn,
                snapshot=snapshot,
                raw_candidates=[{
                    "staging_candidate_id": staging_id,
                    "raw_candidate": row("unissued direction activation"),
                }],
                direction_receipt=self.direction,
            )
            pair_plan = audit.plan_batch_pairs(staged)
            pair_receipt = audit.record_batch_pair_results(
                conn, staged, pair_plan, []
            )
            selected = staged["candidates"][0]
            appended = history_store.append_rows(
                conn, [selected["raw_candidate"]],
                {"run_id": "direction-upgrade-append"},
            )
            candidate = conn.execute(
                "SELECT candidate_id,source_sequence FROM candidates "
                "WHERE candidate_id=?",
                (appended["candidate_ids"][0],),
            ).fetchone()
            activation_sha = "8" * 64
            conn.execute(
                "INSERT INTO audit_activation_receipts VALUES(?, ?, '{}', ?)",
                (
                    activation_sha, staging_id,
                    "2026-08-03T00:00:00Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_activation_maps(
                  staging_candidate_id, legacy_candidate_id, source_sequence,
                  raw_artifact_sha, pair_plan_sha, pair_result_sha,
                  activation_receipt_sha, activated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, '2026-08-03T00:00:00Z')
                """,
                (
                    staging_id, candidate["candidate_id"],
                    candidate["source_sequence"], selected["raw_artifact_sha"],
                    pair_receipt["pair_plan_sha"],
                    pair_receipt["pair_result_sha"], activation_sha,
                ),
            )
            with self.assertRaises(audit_store.AuditMigrationError):
                audit_store.init_schema(conn)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM audit_schema_migrations "
                    "WHERE component='l1-batch-direction-authority'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_unactivated_v2_upgrade_is_migration_v2_and_still_needs_gate(self):
        conn = history_store.connect(self.root / "migration-v2-gate.sqlite3")
        try:
            history_store.init_schema(conn)
            history_store.import_tsv_epoch(conn, self.ledger)
            old_migrations = tuple(
                migration for migration in audit_store.MIGRATIONS
                if migration.component not in {
                    "batch-staging-authority",
                    "batch-direction-gate-authority",
                    "metadata-direction-gate-provenance",
                }
            )
            with mock.patch.object(audit_store, "MIGRATIONS", old_migrations):
                audit_store.init_schema(conn)
            conn.execute(
                """
                INSERT INTO audit_run_manifests(
                  run_id,manifest_schema_version,plan_hash,manifest_json,created_at
                ) VALUES(?, 'history-audit-manifest-v2', ?, '{}',
                         '2026-08-03T00:00:00Z')
                """,
                (self.run_id, self.plan_hash),
            )
            staging_id = self.staging_ids[0]
            snapshot = audit.freeze_snapshot(
                conn,
                run_id=self.run_id,
                batch_id=self.batch_id,
                current_batch_ids=[staging_id],
            )
            normalized = history_store._normalize_append_row(self.raw[0])
            conn.execute(
                """
                INSERT INTO audit_direction_contracts(
                  run_id,batch_id,direction_id,contract_sha,
                  validator_version,artifact_sha,created_at
                ) VALUES(?,?,?,?,?,?, '2026-08-03T00:00:00Z')
                """,
                (
                    self.run_id, self.batch_id, self.direction["direction_id"],
                    self.direction["contract_sha"],
                    self.direction["validator_version"],
                    self.direction["artifact_sha"],
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_batch_staging(
                  staging_candidate_id,run_id,batch_id,candidate_hash,
                  raw_artifact_sha,source_order,created_at
                ) VALUES(?,?,?,?,?,0,'2026-08-03T00:00:00Z')
                """,
                (
                    staging_id, self.run_id, self.batch_id,
                    contract.framed_sha256(
                        "history-candidate-content-v2", normalized
                    ),
                    hashlib.sha256(normalized).hexdigest(),
                ),
            )
            conn.commit()
            audit_store.init_schema(conn)
            self.assertEqual(
                conn.execute(
                    "SELECT authority_kind FROM "
                    "audit_batch_staging_authorities_v2"
                ).fetchone()[0],
                "migration_v2",
            )
            staged = audit.stage_raw_batch(
                conn,
                snapshot=snapshot,
                raw_candidates=[{
                    "staging_candidate_id": staging_id,
                    "raw_candidate": self.raw[0],
                }],
                direction_receipt=self.direction,
            )
            pair_plan = audit.plan_batch_pairs(staged)
            pair_receipt = audit.record_batch_pair_results(
                conn, staged, pair_plan, []
            )
            with self.assertRaisesRegex(ValueError, "direction gate"):
                audit.activate_staged_candidate(
                    conn,
                    snapshot=snapshot,
                    staged_candidate=staged["candidates"][0],
                    pair_receipt=pair_receipt,
                    direction_check={},
                )
            gate = audit.record_batch_direction_gate(
                conn,
                staged_batch=staged,
                direction_receipt=self.direction,
                verdict_tsv=self._direction_tsv(staged),
            )
            activated = audit.activate_staged_candidate(
                conn,
                snapshot=snapshot,
                staged_candidate=staged["candidates"][0],
                pair_receipt=pair_receipt,
                direction_check=gate["verdicts"][0],
            )
            self.assertGreater(
                activated["source_sequence"], snapshot["history_as_of_watermark"]
            )
        finally:
            conn.close()

    def test_unactivated_non_v2_staging_fails_closed_at_authority_upgrade(self):
        conn = history_store.connect(self.root / "staging-fail-closed.sqlite3")
        try:
            history_store.init_schema(conn)
            old_migrations = tuple(
                migration for migration in audit_store.MIGRATIONS
                if migration.component not in {
                    "batch-staging-authority",
                    "batch-direction-gate-authority",
                    "metadata-direction-gate-provenance",
                }
            )
            with mock.patch.object(audit_store, "MIGRATIONS", old_migrations):
                audit_store.init_schema(conn)
            conn.execute(
                """
                INSERT INTO audit_run_manifests(
                  run_id,manifest_schema_version,plan_hash,manifest_json,created_at
                ) VALUES('legacy-run','history-audit-manifest-v2',?,'{}',
                         '2026-08-03T00:00:00Z')
                """,
                ("9" * 64,),
            )
            conn.execute(
                """
                INSERT INTO audit_batch_staging(
                  staging_candidate_id,run_id,batch_id,candidate_hash,
                  raw_artifact_sha,source_order,created_at
                ) VALUES('unactivated-legacy','legacy-run','legacy-batch',
                         ?,?,0,'2026-08-03T00:00:00Z')
                """,
                ("7" * 64, "8" * 64),
            )
            conn.commit()
            with self.assertRaises(audit_store.AuditMigrationError):
                audit_store.init_schema(conn)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM audit_schema_migrations "
                "WHERE component='batch-staging-authority'"
            ).fetchone())
        finally:
            conn.close()

    def test_completed_legacy_staging_migrates_only_as_frozen_replay(self):
        conn = history_store.connect(self.root / "completed-legacy.sqlite3")
        try:
            history_store.init_schema(conn)
            history_store.import_tsv_epoch(conn, self.ledger)
            old_migrations = tuple(
                migration for migration in audit_store.MIGRATIONS
                if migration.component not in {
                    "batch-staging-authority",
                    "batch-direction-gate-authority",
                    "metadata-direction-gate-provenance",
                }
            )
            with mock.patch.object(audit_store, "MIGRATIONS", old_migrations):
                audit_store.init_schema(conn)
            conn.execute(
                """
                INSERT INTO audit_run_manifests(
                  run_id,manifest_schema_version,plan_hash,manifest_json,created_at
                ) VALUES('legacy-run','history-audit-manifest-v2',?,'{}',
                         '2026-08-03T00:00:00Z')
                """,
                ("9" * 64,),
            )
            conn.execute(
                """
                INSERT INTO audit_direction_contracts(
                  run_id,batch_id,direction_id,contract_sha,
                  validator_version,artifact_sha,created_at
                ) VALUES('legacy-run','legacy-batch',?,?,?,?,
                         '2026-08-03T00:00:00Z')
                """,
                (
                    self.direction["direction_id"],
                    self.direction["contract_sha"],
                    self.direction["validator_version"],
                    self.direction["artifact_sha"],
                ),
            )
            legacy_rows = [row("completed legacy one"), row("completed legacy two")]
            appended = history_store.append_rows(
                conn, legacy_rows, {"run_id": "legacy-append"}
            )
            pair_plan_sha = "a" * 64
            pair_result_sha = "b" * 64
            staging_material = []
            for index, (staging_id, raw_value, candidate_id) in enumerate(zip(
                ("legacy-stage-one", "legacy-stage-two"),
                legacy_rows,
                appended["candidate_ids"],
            )):
                normalized = history_store._normalize_append_row(raw_value)
                raw_sha = hashlib.sha256(normalized).hexdigest()
                candidate_hash = contract.framed_sha256(
                    "history-candidate-content-v2", normalized
                )
                conn.execute(
                    """
                    INSERT INTO audit_batch_staging(
                      staging_candidate_id,run_id,batch_id,candidate_hash,
                      raw_artifact_sha,source_order,created_at
                    ) VALUES(?,'legacy-run','legacy-batch',?,?,?,
                             '2026-08-03T00:00:00Z')
                    """,
                    (staging_id, candidate_hash, raw_sha, index),
                )
                conn.execute(
                    """
                    INSERT INTO audit_direction_checks(
                      run_id,batch_id,direction_id,contract_sha,
                      validator_version,artifact_sha,staging_candidate_id,
                      semantic_relation,lineage_relation,evidence_sha,checked_at
                    ) VALUES('legacy-run','legacy-batch',?,?,?,?,?,
                             'distinct','none',?,'2026-08-03T00:00:00Z')
                    """,
                    (
                        self.direction["direction_id"],
                        self.direction["contract_sha"],
                        self.direction["validator_version"],
                        self.direction["artifact_sha"], staging_id, "6" * 64,
                    ),
                )
                candidate = conn.execute(
                    "SELECT source_sequence FROM candidates WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                activation_sha = hashlib.sha256(
                    ("legacy-activation:" + staging_id).encode()
                ).hexdigest()
                conn.execute(
                    "INSERT INTO audit_activation_receipts VALUES(?,?, '{}',?)",
                    (
                        activation_sha, staging_id,
                        "2026-08-03T00:00:00Z",
                    ),
                )
                staging_material.append((
                    staging_id, candidate_id, candidate["source_sequence"],
                    raw_sha, candidate_hash, index, activation_sha,
                ))
            conn.execute(
                """
                INSERT INTO audit_batch_pairs(
                  run_id,batch_id,left_staging_candidate_id,
                  right_staging_candidate_id,pair_plan_sha,pair_result_sha,
                  created_at
                ) VALUES('legacy-run','legacy-batch','legacy-stage-one',
                         'legacy-stage-two',?,?,'2026-08-03T00:00:00Z')
                """,
                (pair_plan_sha, pair_result_sha),
            )
            for (
                staging_id, candidate_id, source_sequence, raw_sha,
                _candidate_hash, _source_order, activation_sha,
            ) in staging_material:
                conn.execute(
                    """
                    INSERT INTO audit_activation_maps(
                      staging_candidate_id,legacy_candidate_id,source_sequence,
                      raw_artifact_sha,pair_plan_sha,pair_result_sha,
                      activation_receipt_sha,activated_at
                    ) VALUES(?,?,?,?,?,?,?,'2026-08-03T00:00:00Z')
                    """,
                    (
                        staging_id, candidate_id, source_sequence, raw_sha,
                        pair_plan_sha, pair_result_sha, activation_sha,
                    ),
                )
            conn.commit()
            audit_store.init_schema(conn)
            self.assertEqual(
                [row[0] for row in conn.execute(
                    "SELECT authority_kind FROM "
                    "audit_batch_staging_authorities_v2 ORDER BY source_order"
                )],
                ["migration_legacy", "migration_legacy"],
            )
            first = staging_material[0]
            conn.execute("BEGIN IMMEDIATE")
            audit_store.insert_authorized_batch_staging(
                conn,
                staging_candidate_id=first[0],
                run_id="legacy-run",
                batch_id="legacy-batch",
                candidate_hash=first[4],
                raw_artifact_sha=first[3],
                source_order=first[5],
            )
            conn.execute("COMMIT")
            replay = audit.record_direction_check(
                conn,
                staged_candidate={"staging_candidate_id": first[0]},
                direction_receipt=self.direction,
                semantic_relation="distinct",
                lineage_relation="none",
                evidence_sha="6" * 64,
            )
            self.assertEqual(replay["staging_candidate_id"], first[0])
            with self.assertRaisesRegex(
                ValueError, "conflicts with durable state"
            ):
                audit.record_direction_check(
                    conn,
                    staged_candidate={"staging_candidate_id": first[0]},
                    direction_receipt=self.direction,
                    semantic_relation="uncertain",
                    lineage_relation="none",
                    evidence_sha="7" * 64,
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
        direction_check = self._direction_verdicts(staged)[0]
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

    def test_activation_requires_direction_verdict_for_every_batch_member(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        pair_plan = audit.plan_batch_pairs(staged)
        pair_results = [
            {
                "left_staging_candidate_id": pair["left_staging_candidate_id"],
                "right_staging_candidate_id": pair["right_staging_candidate_id"],
                "semantic_relation": "distinct",
                "evidence_sha": "6" * 64,
            }
            for pair in pair_plan["pairs"]
        ]
        pair_receipt = audit.record_batch_pair_results(
            self.conn, staged, pair_plan, pair_results
        )
        with self.assertRaises(ValueError):
            audit.record_batch_direction_gate(
                self.conn,
                staged_batch=staged,
                direction_receipt=self.direction,
                verdict_tsv=(
                    b"id\tdirection-fit\tdirection-evidence\n"
                    b"I1\tin-scope\tonly one member was checked\n"
                ),
            )
        direction_check = {}
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

    def test_one_out_of_scope_direction_verdict_rejects_the_whole_batch(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        pair_plan = audit.plan_batch_pairs(staged)
        pair_results = [
            {
                "left_staging_candidate_id": pair["left_staging_candidate_id"],
                "right_staging_candidate_id": pair["right_staging_candidate_id"],
                "semantic_relation": "distinct",
                "evidence_sha": hashlib.sha256((
                    pair["left_staging_candidate_id"]
                    + pair["right_staging_candidate_id"]
                ).encode()).hexdigest(),
            }
            for pair in pair_plan["pairs"]
        ]
        pair_receipt = audit.record_batch_pair_results(
            self.conn, staged, pair_plan, pair_results
        )
        checks = self._direction_verdicts(
            staged, ["in-scope", "out-of-scope"]
        )
        before = self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
        with self.assertRaises(ValueError):
            audit.activate_staged_candidate(
                self.conn,
                snapshot=snapshot,
                staged_candidate=staged["candidates"][0],
                pair_receipt=pair_receipt,
                direction_check=checks[0],
            )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
            before,
        )

    def test_direction_verdict_is_host_canonical_and_append_only(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        candidate = staged["candidates"][0]
        evidence = "independent selector canonical evidence"
        gate = audit.record_batch_direction_gate(
            self.conn,
            staged_batch=staged,
            direction_receipt=self.direction,
            verdict_tsv=self._direction_tsv(
                staged, evidences=[evidence, "second canonical evidence"]
            ),
        )
        verdict = next(
            item for item in gate["verdicts"]
            if item["staging_candidate_id"] == candidate["staging_candidate_id"]
        )
        material = {
            "schema_version": "history-direction-verdict-v2",
            "run_id": staged["run_id"],
            "batch_id": staged["batch_id"],
            "snapshot_id": snapshot["snapshot_id"],
            "current_batch_ids_hash": snapshot["current_batch_ids_hash"],
            **self.direction,
            "staging_candidate_id": candidate["staging_candidate_id"],
            "direction_fit": "in-scope",
            "direction_evidence": evidence,
        }
        self.assertEqual(
            verdict["verdict_sha256"],
            contract.framed_sha256(
                "history-direction-verdict-v2",
                contract.canonical_bytes(material),
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM audit_batch_direction_verdicts_v2 "
            "WHERE verdict_sha256=?",
            (verdict["verdict_sha256"],),
        ).fetchone()
        self.assertEqual(
            contract.parse_json_bytes(row["evidence_json"].encode()), evidence
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE audit_batch_direction_verdicts_v2 "
                "SET direction_fit='out-of-scope' WHERE verdict_sha256=?",
                (verdict["verdict_sha256"],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM audit_batch_direction_gates_v2 WHERE gate_sha256=?",
                (gate["gate_sha256"],),
            )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "direction gate requires host issuance"
        ):
            self.conn.execute(
                "INSERT INTO audit_batch_direction_gates_v2 "
                "SELECT * FROM audit_batch_direction_gates_v2 WHERE gate_sha256=?",
                (gate["gate_sha256"],),
            )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "direction gate binding requires host issuance"
        ):
            self.conn.execute(
                "INSERT INTO audit_batch_direction_gate_bindings_v2 "
                "SELECT * FROM audit_batch_direction_gate_bindings_v2 "
                "WHERE gate_sha256=? LIMIT 1",
                (gate["gate_sha256"],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM audit_batch_direction_verdicts_v2 "
                "WHERE verdict_sha256=?",
                (verdict["verdict_sha256"],),
            )
        with self.assertRaises(ValueError):
            audit.record_direction_check(
                self.conn,
                staged_candidate=candidate,
                direction_receipt=self.direction,
                direction_fit="out-of-scope",
                direction_evidence="conflicting replay",
            )

    def test_legacy_semantic_direction_checks_do_not_satisfy_batch_gate(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        _, pair_receipt = self._pair_evidence(staged)
        for candidate in staged["candidates"]:
            with self.assertRaisesRegex(
                ValueError, "legacy direction migration boundary is immutable"
            ):
                audit.record_direction_check(
                self.conn,
                staged_candidate=candidate,
                direction_receipt=self.direction,
                semantic_relation="distinct",
                lineage_relation="none",
                evidence_sha="6" * 64,
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM audit_batch_direction_verdicts_v2"
            ).fetchone()[0],
            0,
        )
        with self.assertRaises(ValueError):
            audit.activate_staged_candidate(
                self.conn,
                snapshot=snapshot,
                staged_candidate=staged["candidates"][0],
                pair_receipt=pair_receipt,
                direction_check={},
            )

    def test_database_batch_direction_gate_rejects_missing_member_verdict(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        _, pair_receipt = self._pair_evidence(staged)
        with self.assertRaises(ValueError):
            audit.record_batch_direction_gate(
                self.conn,
                staged_batch=staged,
                direction_receipt=self.direction,
                verdict_tsv=(
                    b"id\tdirection-fit\tdirection-evidence\n"
                    b"I1\tin-scope\tonly one member was checked\n"
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self._direct_activation_insert(staged["candidates"][0], pair_receipt)

    def test_database_batch_direction_gate_rejects_any_out_of_scope_verdict(self):
        snapshot = self._snapshot()
        staged = self._staged(snapshot=snapshot)
        _, pair_receipt = self._pair_evidence(staged)
        self._direction_verdicts(staged, ["in-scope", "out-of-scope"])
        with self.assertRaises(sqlite3.IntegrityError):
            self._direct_activation_insert(staged["candidates"][0], pair_receipt)

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

    def test_l1_positive_receipt_rejects_caller_only_verified_hit(self):
        snapshot = self._snapshot()
        retrieval, adjudication, qualification = self._receipt_inputs(snapshot)
        forged = dict(
            adjudication,
            verified_hits=[{
                "lineage_id": "caller-invented-lineage",
                "source": "normalized_exact",
                "semantic_relation": "blocking_duplicate",
            }],
        )
        with self.assertRaises(ValueError):
            audit.build_l1_receipt(
                snapshot,
                retrieval,
                forged,
                qualification,
                qualification_conn=self.conn,
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
