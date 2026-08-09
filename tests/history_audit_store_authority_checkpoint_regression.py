#!/usr/bin/env python3
"""Regression coverage for receipt and settlement authority sidecars."""

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

from lib import history_audit
from lib import history_audit_store
from lib import history_contract_v2
from lib import history_store


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"


def raw_row(story):
    return (
        "2026-08-09\thunt\tAuthority\t" + story
        + "\taccept-w-rev\treason\tlow\tdesign-fixable\n"
    ).encode("utf-8")


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ReceiptAuthorityCheckpointRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text(
            "authority-checkpoint\n", encoding="utf-8"
        )
        self.ledger = self.root / "ledger.tsv"
        self.ledger.write_bytes(HEADER + raw_row("prior"))
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)
        history_store.import_tsv_epoch(self.conn, self.ledger)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _install_run(self, run_id):
        self.conn.execute(
            """
            INSERT INTO audit_run_manifests(
              run_id,manifest_schema_version,plan_hash,manifest_json,created_at
            ) VALUES(?, 'history-audit-manifest-v2', ?, '{}',
                     '2026-08-09T00:00:00+00:00')
            """,
            (run_id, sha(run_id)),
        )

    def _stage(self, run_id, batch_id, marker):
        self._install_run(run_id)
        staging_ids = ["stg-v2-" + marker * 64, "stg-v2-" + chr(ord(marker) + 1) * 64]
        snapshot = history_audit.freeze_snapshot(
            self.conn,
            run_id=run_id,
            batch_id=batch_id,
            current_batch_ids=staging_ids,
        )
        direction = {
            "direction_id": "direction-" + marker,
            "contract_sha": sha("contract-" + marker),
            "validator_version": "direction-validator-v1",
            "artifact_sha": sha("artifact-" + marker),
        }
        staged = history_audit.stage_raw_batch(
            self.conn,
            snapshot=snapshot,
            raw_candidates=[
                {"staging_candidate_id": staging_ids[0], "raw_candidate": raw_row(marker + "-0")},
                {"staging_candidate_id": staging_ids[1], "raw_candidate": raw_row(marker + "-1")},
            ],
            direction_receipt=direction,
        )
        return snapshot, staged, direction

    def _record_pairs(self, staged):
        plan = history_audit.plan_batch_pairs(staged)
        results = [
            {
                "left_staging_candidate_id": pair["left_staging_candidate_id"],
                "right_staging_candidate_id": pair["right_staging_candidate_id"],
                "semantic_relation": "distinct",
                "evidence_sha": sha(pair["left_staging_candidate_id"] + pair["right_staging_candidate_id"]),
            }
            for pair in plan["pairs"]
        ]
        return history_audit.record_batch_pair_results(
            self.conn, staged, plan, results
        )

    def _direction_gate(self, staged, direction):
        rows = ["id\tdirection-fit\tdirection-evidence"]
        rows.extend(
            f"I{index + 1}\tin-scope\tevidence-{index + 1}"
            for index, _ in enumerate(
                sorted(staged["candidates"], key=lambda item: item["source_order"])
            )
        )
        return history_audit.record_batch_direction_gate(
            self.conn,
            staged_batch=staged,
            direction_receipt=direction,
            verdict_tsv=("\n".join(rows) + "\n").encode("utf-8"),
        )

    def test_pair_migration_quarantines_until_canonical_evidence_replays(self):
        migrations = tuple(
            migration for migration in history_audit_store.MIGRATIONS
            if migration.component not in {
                "core-authority-repair", "pair-result-authority",
            }
        )
        with (
            mock.patch.object(history_audit_store, "MIGRATIONS", migrations),
            mock.patch.object(
                history_audit_store, "issue_batch_pair_result_authority",
                return_value=None,
            ),
        ):
            history_audit_store.init_schema(self.conn)
            _, complete, _ = self._stage(
                "run-complete", "batch-complete", "2"
            )
            complete_receipt = self._record_pairs(complete)
        snapshot, invalid, _ = self._stage("run-invalid", "batch-invalid", "4")
        self.conn.execute(
            """
            INSERT INTO audit_batch_pair_receipts(
              run_id,batch_id,snapshot_id,pair_plan_sha,pair_result_sha,
              pair_count,completed_at
            ) VALUES(?,?,?,?,?,1,'2026-08-09T00:00:00+00:00')
            """,
            (
                invalid["run_id"], invalid["batch_id"], snapshot["snapshot_id"],
                sha("wrong-plan"), sha("wrong-result"),
            ),
        )
        self.conn.commit()

        history_audit_store.init_schema(self.conn)
        quarantined = {
            row[0] for row in self.conn.execute(
                "SELECT run_id FROM audit_batch_pair_receipt_quarantine_v4"
            )
        }
        self.assertEqual(quarantined, {"run-complete", "run-invalid"})
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM audit_valid_batch_pair_receipt_authority_v4 "
                "WHERE run_id='run-complete'"
            ).fetchone()
        )

        replay = self._record_pairs(complete)
        self.assertEqual(replay, complete_receipt)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM audit_valid_batch_pair_receipt_authority_v4 "
                "WHERE run_id='run-complete'"
            ).fetchone()
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM audit_valid_batch_pair_receipt_authority_v4 "
                "WHERE run_id='run-invalid'"
            ).fetchone()
        )
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ValueError):
            history_audit_store.issue_batch_pair_result_authority(
                self.conn,
                run_id="run-complete",
                batch_id="batch-complete",
                pair_plan_sha=replay["pair_plan_sha"],
                pair_result_sha=replay["pair_result_sha"],
                results=[],
            )
        self.conn.execute("ROLLBACK")

        self.conn.execute(
            "DROP TRIGGER audit_batch_pair_set_bindings_immutable_delete"
        )
        self.conn.execute(
            "DELETE FROM audit_batch_pair_set_bindings "
            "WHERE run_id='run-complete' AND batch_id='batch-complete'"
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM audit_valid_batch_pair_receipt_authority_v4 "
                "WHERE run_id='run-complete'"
            ).fetchone()
        )

    def test_activation_authority_binds_canonical_receipt_and_mapping(self):
        history_audit_store.init_schema(self.conn)
        snapshot, staged, direction = self._stage("run-active", "batch-active", "6")
        pair = self._record_pairs(staged)
        gate = self._direction_gate(staged, direction)
        activated = history_audit.activate_staged_candidate(
            self.conn,
            snapshot=snapshot,
            staged_candidate=staged["candidates"][0],
            pair_receipt=pair,
            direction_check=gate["verdicts"][0],
        )
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM audit_valid_activation_receipt_authority_v3 "
                "WHERE staging_candidate_id=?",
                (staged["candidates"][0]["staging_candidate_id"],),
            ).fetchone()
        )
        self.assertEqual(activated["source_sequence"] > snapshot["history_as_of_watermark"], True)

        forged_json = "{}\n"
        forged_sha = hashlib.sha256(forged_json.encode("utf-8")).hexdigest()
        self.conn.execute(
            "INSERT INTO audit_activation_receipts VALUES(?,?,?,?)",
            (
                forged_sha, staged["candidates"][1]["staging_candidate_id"],
                forged_json, "2026-08-09T00:00:00+00:00",
            ),
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM audit_valid_activation_receipt_authority_v3 "
                "WHERE activation_receipt_sha=?", (forged_sha,),
            ).fetchone()
        )


class SettlementAuthorityCheckpointRegression(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        history_audit_store.init_schema(self.conn)
        for table in (
            "audit_batch_staging", "audit_logical_tasks", "audit_task_bindings_v2",
            "audit_task_attempts", "audit_attempt_completions_v2",
        ):
            triggers = list(self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (table,),
            ))
            for trigger in triggers:
                self.conn.execute(f'DROP TRIGGER "{trigger[0]}"')
        self.task = "1" * 64
        self.attempts = ["2" * 64, "3" * 64]
        self.outputs = ["4" * 64, "5" * 64]
        self._install_rows()

    def tearDown(self):
        self.conn.close()

    def _install_rows(self):
        now = "2026-08-09T00:00:00+00:00"
        self.conn.execute(
            "INSERT INTO audit_run_manifests VALUES(?, 'history-audit-manifest-v2', ?, '{}', ?)",
            ("run-settlement", "6" * 64, now),
        )
        self.conn.execute(
            "INSERT INTO audit_batch_staging VALUES(?,?,?,?,?,?,?)",
            ("stg-v2-" + "7" * 64, "run-settlement", "batch", "8" * 64, "9" * 64, 0, now),
        )
        self.conn.execute(
            "INSERT INTO audit_logical_tasks VALUES(?,?,?,?,?,'settling',1,?,?,?)",
            (self.task, "run-settlement", "map", "stg-v2-" + "7" * 64, "input", "claim", "2099-01-01T00:00:00+00:00", now),
        )
        for index, object_id in enumerate(["a" * 64] + self.outputs):
            self.conn.execute(
                "INSERT INTO audit_cas_objects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (object_id, sha("raw-" + str(index)), sha("compressed-" + str(index)), "zlib-v1", 1, 1, "test", f"{index}.z", now, None, "verified"),
            )
        for ordinal, (attempt_id, output_id, value) in enumerate(
            zip(self.attempts, self.outputs, (1, 2))
        ):
            self.conn.execute(
                "INSERT INTO audit_task_attempts VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, self.task, ordinal, "{}", "a" * 64, output_id, "completed", now),
            )
            self.conn.execute(
                "INSERT INTO audit_attempt_completions_v2 VALUES(?,?,'valid',?,?,?)",
                (attempt_id, output_id, self._canonical({"value": value}), "{}", now),
            )

    @staticmethod
    def _canonical(value):
        return history_contract_v2.canonical_bytes(value).decode("utf-8")

    def _material(self, attempt_ids, outputs, *, kind="conflict", result=None):
        material = {
            "task_hash": self.task,
            "settlement_kind": kind,
            "normalized_result": result,
            "valid_attempt_ids": attempt_ids,
            "valid_output_cas_ids": outputs,
        }
        return history_contract_v2.framed_sha256(
            "history-task-settlement-v2",
            history_contract_v2.canonical_bytes(material),
        )

    def _insert(self, attempt_ids, outputs, *, kind="conflict", result=None):
        self.conn.execute(
            """
            INSERT INTO audit_task_settlements_v2(
              task_hash,settlement_sha256,settlement_kind,normalized_result_json,
              valid_attempt_ids_json,valid_output_cas_ids_json,settled_at
            ) VALUES(?,?,?,?,?,?, '2026-08-09T00:00:00+00:00')
            """,
            (
                self.task, self._material(attempt_ids, outputs, kind=kind, result=result),
                kind, None if result is None else self._canonical(result),
                self._canonical(attempt_ids), self._canonical(outputs),
            ),
        )

    def test_settlement_rejects_output_swap_and_omitted_valid_attempt(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(self.attempts, list(reversed(self.outputs)))
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(self.attempts[:1], self.outputs[:1], kind="equal", result={"value": 1})
        self._insert(self.attempts, self.outputs)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM audit_valid_task_settlement_authority_v3 "
                "WHERE task_hash=?", (self.task,),
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
