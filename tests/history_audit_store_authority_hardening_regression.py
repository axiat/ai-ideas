#!/usr/bin/env python3
"""Focused regressions for authority input and quarantine hardening."""

import hashlib
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from lib import history_audit
from lib import history_audit_store
from lib import history_contract_v2
from lib import history_store
import history_audit_store_authority_checkpoint_regression as authority_fixture


HEADER = b"date\tsource\ttheme\tidea\tverdict\treason\toverlap\tcategory\n"
NOW = "2026-08-09T00:00:00+00:00"


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def canonical(value):
    return history_contract_v2.canonical_bytes(value).decode("utf-8")


class AuthorityMigrationInputHardeningRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        (self.root / "ledger.instance-id").write_text(
            "authority-input-hardening\n", encoding="utf-8"
        )
        (self.root / "ledger.tsv").write_bytes(HEADER)
        self.conn = history_store.connect(self.root / "history.sqlite3")
        history_store.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    @staticmethod
    def _without_authority_repairs():
        return tuple(
            migration for migration in history_audit_store.MIGRATIONS
            if migration.component not in {
                "core-authority-repair",
                "pair-result-authority",
                "authority-input-hardening",
            }
        )

    @staticmethod
    def _drop_triggers(conn, *tables):
        definitions = []
        for table in tables:
            for row in list(conn.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name=?",
                (table,),
            )):
                definitions.append(row[1])
                conn.execute(f'DROP TRIGGER "{row[0]}"')
        return definitions

    @staticmethod
    def _restore_triggers(conn, definitions):
        for statement in definitions:
            conn.execute(statement)

    def _install_legacy_task(self, task_hash):
        self.conn.execute(
            """
            INSERT OR IGNORE INTO audit_run_manifests(
              run_id,manifest_schema_version,plan_hash,manifest_json,created_at
            ) VALUES('run-hardening','history-audit-manifest-v2',?,'{}',?)
            """,
            ("1" * 64, NOW),
        )
        self.conn.execute(
            """
            INSERT INTO audit_logical_tasks(
              task_hash,run_id,stage,staging_candidate_id,input_id,
              state,fence,claim_token,lease_until,created_at
            ) VALUES(?,'run-hardening','map',?,'input','settling',1,
                     'claim','2099-01-01T00:00:00+00:00',?)
            """,
            (task_hash, "stg-v2-" + "2" * 64, NOW),
        )

    def test_malformed_settlement_json_is_quarantined_without_aborting_init(self):
        with mock.patch.object(
            history_audit_store,
            "MIGRATIONS",
            self._without_authority_repairs(),
        ):
            history_audit_store.init_schema(self.conn)
        trigger_definitions = self._drop_triggers(
            self.conn, "audit_logical_tasks"
        )
        cases = (
            ("3" * 64, "[1]\n", canonical(["a" * 64]), canonical({"value": 1})),
            ("4" * 64, canonical(["b" * 64]), canonical(["c" * 64]), '{"value":NaN}\n'),
            ("5" * 64, canonical(["d" * 64]), canonical(["e" * 64]), '{"value":1.5}\n'),
        )
        for task_hash, attempt_ids, output_ids, normalized in cases:
            self._install_legacy_task(task_hash)
            self.conn.execute(
                """
                INSERT INTO audit_task_settlements_v2(
                  task_hash,settlement_sha256,settlement_kind,
                  normalized_result_json,valid_attempt_ids_json,
                  valid_output_cas_ids_json,settled_at
                ) VALUES(?,?,'equal',?,?,?,?)
                """,
                (
                    task_hash, sha("settlement-" + task_hash), normalized,
                    attempt_ids, output_ids, NOW,
                ),
            )
        self._restore_triggers(self.conn, trigger_definitions)
        self.conn.commit()

        history_audit_store.init_schema(self.conn)
        quarantined = {
            row[0] for row in self.conn.execute(
                "SELECT task_hash FROM audit_task_settlement_authority_v3 "
                "WHERE event_state='quarantined'"
            )
        }
        self.assertEqual(quarantined, {case[0] for case in cases})
        for _, attempt_ids, output_ids, normalized in cases:
            self.assertEqual(
                history_audit_store._task_settlement_material_valid(
                    "f" * 64, "0" * 64, "equal", normalized,
                    attempt_ids, output_ids,
                ),
                0,
            )

    def _noncanonical_conflict_completion_is_not_authoritative(self):
        with mock.patch.object(
            history_audit_store,
            "MIGRATIONS",
            self._without_authority_repairs(),
        ):
            history_audit_store.init_schema(self.conn)
        trigger_definitions = self._drop_triggers(
            self.conn,
            "audit_logical_tasks",
            "audit_task_attempts",
            "audit_attempt_completions_v2",
        )
        task_hash = "6" * 64
        self._install_legacy_task(task_hash)
        request_id = "7" * 64
        outputs = ["8" * 64, "9" * 64]
        attempts = ["a" * 64, "b" * 64]
        for index, object_id in enumerate([request_id] + outputs):
            self.conn.execute(
                "INSERT INTO audit_cas_objects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    object_id, sha(f"raw-{index}"), sha(f"compressed-{index}"),
                    "zlib-v1", 1, 1, "test", f"{index}.z", NOW, None,
                    "verified",
                ),
            )
        results = [canonical({"value": 1}), '{"value": 1}\n']
        for ordinal, (attempt_id, output_id, result) in enumerate(
            zip(attempts, outputs, results)
        ):
            self.conn.execute(
                "INSERT INTO audit_task_attempts VALUES(?,?,?,?,?,?,?,?)",
                (
                    attempt_id, task_hash, ordinal, "{}", request_id,
                    output_id, "completed", NOW,
                ),
            )
            self.conn.execute(
                "INSERT INTO audit_attempt_completions_v2 "
                "VALUES(?,?,'valid',?,'{}',?)",
                (attempt_id, output_id, result, NOW),
            )
        material = {
            "task_hash": task_hash,
            "settlement_kind": "conflict",
            "normalized_result": None,
            "valid_attempt_ids": attempts,
            "valid_output_cas_ids": outputs,
        }
        settlement_sha = history_contract_v2.framed_sha256(
            "history-task-settlement-v2",
            history_contract_v2.canonical_bytes(material),
        )
        self.conn.execute(
            """
            INSERT INTO audit_task_settlements_v2(
              task_hash,settlement_sha256,settlement_kind,
              normalized_result_json,valid_attempt_ids_json,
              valid_output_cas_ids_json,settled_at
            ) VALUES(?,?,'conflict',NULL,?,?,?)
            """,
            (task_hash, settlement_sha, canonical(attempts), canonical(outputs), NOW),
        )
        self._restore_triggers(self.conn, trigger_definitions)
        self.conn.commit()

        history_audit_store.init_schema(self.conn)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM audit_attempt_completion_quarantine_v4 "
                "WHERE attempt_id=?", (attempts[1],),
            ).fetchone()
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM audit_valid_task_settlement_authority_v5 "
                "WHERE task_hash=?", (task_hash,),
            ).fetchone()
        )

    def test_skipped_prerequisite_migrations_are_not_ledgered(self):
        migrations = tuple(
            migration for migration in history_audit_store.MIGRATIONS
            if migration.component != "l1-strict-pair-completion"
        )
        with mock.patch.object(history_audit_store, "MIGRATIONS", migrations):
            history_audit_store.init_schema(self.conn)
        ledgered = {
            row[0] for row in self.conn.execute(
                "SELECT component FROM audit_schema_migrations"
            )
        }
        self.assertNotIn("core-authority-repair", ledgered)
        self.assertNotIn("pair-result-authority", ledgered)
        self.assertNotIn("authority-input-hardening", ledgered)


class SettlementCanonicalConflictRegression(unittest.TestCase):
    def setUp(self):
        self.fixture = authority_fixture.SettlementAuthorityCheckpointRegression(
            "test_settlement_rejects_output_swap_and_omitted_valid_attempt"
        )
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_semantically_equal_whitespace_result_is_not_conflict_authority(self):
        self.fixture._insert(self.fixture.attempts, self.fixture.outputs)
        self.assertIsNotNone(
            self.fixture.conn.execute(
                "SELECT 1 FROM audit_valid_task_settlement_authority_v5 "
                "WHERE task_hash=?", (self.fixture.task,),
            ).fetchone()
        )
        for row in list(self.fixture.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='audit_attempt_completions_v2'"
        )):
            self.fixture.conn.execute(f'DROP TRIGGER "{row[0]}"')
        self.fixture.conn.execute(
            "UPDATE audit_attempt_completions_v2 "
            "SET normalized_result_json=? WHERE attempt_id=?",
            ('{"value": 1}\n', self.fixture.attempts[1]),
        )
        self.assertEqual(
            history_audit_store._normalized_result_json_valid('{"value": 1}\n'),
            0,
        )
        self.assertIsNone(
            self.fixture.conn.execute(
                "SELECT 1 FROM audit_valid_task_settlement_authority_v5 "
                "WHERE task_hash=?", (self.fixture.task,),
            ).fetchone()
        )


class ActivationCandidateAndQuarantineRegression(unittest.TestCase):
    def setUp(self):
        self.fixture = authority_fixture.ReceiptAuthorityCheckpointRegression(
            "test_activation_authority_binds_canonical_receipt_and_mapping"
        )
        self.fixture.setUp()
        history_audit_store.init_schema(self.fixture.conn)

    def tearDown(self):
        self.fixture.tearDown()

    def _activate(self):
        snapshot, staged, direction = self.fixture._stage(
            "run-content", "batch-content", "6"
        )
        pair = self.fixture._record_pairs(staged)
        gate = self.fixture._direction_gate(staged, direction)
        activated = history_audit.activate_staged_candidate(
            self.fixture.conn,
            snapshot=snapshot,
            staged_candidate=staged["candidates"][0],
            pair_receipt=pair,
            direction_check=gate["verdicts"][0],
        )
        receipt = self.fixture.conn.execute(
            "SELECT activation_receipt_sha FROM audit_activation_maps "
            "WHERE staging_candidate_id=?",
            (staged["candidates"][0]["staging_candidate_id"],),
        ).fetchone()[0]
        return activated, receipt

    def test_activation_authority_rechecks_candidate_durable_content(self):
        activated, receipt_sha = self._activate()
        self.assertIsNotNone(
            self.fixture.conn.execute(
                "SELECT 1 FROM audit_valid_activation_receipt_authority_v5 "
                "WHERE activation_receipt_sha=?", (receipt_sha,),
            ).fetchone()
        )
        for row in list(self.fixture.conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='candidates'"
        )):
            self.fixture.conn.execute(f'DROP TRIGGER "{row[0]}"')
        self.fixture.conn.execute(
            "UPDATE candidates SET raw_sha256=? WHERE candidate_id=?",
            ("f" * 64, activated["legacy_candidate_id"]),
        )
        self.assertIsNone(
            self.fixture.conn.execute(
                "SELECT 1 FROM audit_valid_activation_receipt_authority_v5 "
                "WHERE activation_receipt_sha=?", (receipt_sha,),
            ).fetchone()
        )

    def test_forged_quarantine_hash_cannot_suppress_valid_receipt(self):
        _, receipt_sha = self._activate()
        receipt = self.fixture.conn.execute(
            "SELECT * FROM audit_activation_receipts "
            "WHERE activation_receipt_sha=?", (receipt_sha,),
        ).fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.fixture.conn.execute(
                """
                INSERT INTO audit_activation_receipt_authority_v3(
                  activation_receipt_sha,event_state,reason,
                  authority_sha256,created_at
                ) VALUES(?,'quarantined','legacy_activation_receipt_invalid',?,?)
                """,
                (receipt_sha, "0" * 64, receipt["created_at"]),
            )
        self.fixture.conn.execute(
            "DROP TRIGGER audit_activation_receipt_authority_insert_guard_v5"
        )
        self.fixture.conn.execute(
            """
            INSERT INTO audit_activation_receipt_authority_v3(
              activation_receipt_sha,event_state,reason,
              authority_sha256,created_at
            ) VALUES(?,'quarantined','legacy_activation_receipt_invalid',?,?)
            """,
            (receipt_sha, "0" * 64, receipt["created_at"]),
        )
        self.assertIsNotNone(
            self.fixture.conn.execute(
                "SELECT 1 FROM audit_valid_activation_receipt_authority_v5 "
                "WHERE activation_receipt_sha=?", (receipt_sha,),
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
